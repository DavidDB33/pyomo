#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

"""
Cutting plane-based GDP reformulation.

Implements a general cutting plane-based reformulation for linear and
convex GDPs.
"""
from __future__ import division
try:
    from collections import OrderedDict
except:
    from ordereddict import OrderedDict


from pyomo.common.config import (ConfigBlock, ConfigValue, PositiveFloat,
                                 NonNegativeFloat)
from pyomo.common.modeling import unique_component_name
from pyomo.core import ( Any, Block, Constraint, Objective, Param, Var,
                         SortComponents, Transformation, TransformationFactory,
                         value, TransformationFactory, NonNegativeIntegers,
                         Reals )
from pyomo.core.expr import differentiate
from pyomo.core.base.component import ComponentUID
from pyomo.core.expr.current import identify_variables
from pyomo.repn.standard_repn import generate_standard_repn
from pyomo.core.kernel.component_map import ComponentMap
from pyomo.core.kernel.component_set import ComponentSet
from pyomo.opt import SolverFactory

from pyomo.gdp import Disjunct, Disjunction, GDP_Error
from pyomo.gdp.util import ( verify_successful_solve, NORMAL, INFEASIBLE,
                             NONOPTIMAL, clone_without_expression_components )

from pyomo.contrib.fme.fourier_motzkin_elimination import \
    Fourier_Motzkin_Elimination_Transformation

from six import iterkeys, itervalues, iteritems
from numpy import isclose

import math
import logging
logger = logging.getLogger('pyomo.gdp.cuttingplane')

NAME_BUFFER = {}

def do_not_tighten(m):
    return m

def _get_constraint_exprs(constraints, hull_to_bigm_map):
    cuts = []
    for cons in constraints:
        cuts.append(clone_without_expression_components( 
            cons.expr, substitute=hull_to_bigm_map))
    return cuts

def _constraint_tight(model, constraint):
    val = value(constraint.body)
    ans = 0
    if constraint.lower is not None:
        if value(constraint.lower) >= val:
            # tight or in violation of LB
            ans -= 1

    if constraint.upper is not None:
        if value(constraint.upper) <= val:
            # tight or in violation of UB
            ans += 1

    return ans

def _get_linear_approximation_expr(normal_vec, point):
    body = 0
    for coef, v in zip(point, normal_vec):
        body -= coef*v
    return body >= -sum(normal_vec[idx]*v.value for (idx, v) in
                       enumerate(point))

def create_cuts_fme(var_info, hull_to_bigm_map, disaggregated_vars,
                     disaggregation_constraints, rHull_vars, instance_rHull,
                     rBigM_linear_constraints, transBlock_rBigm,
                     transBlock_rHull, cut_threshold, zero_tolerance):
    """Returns a cut which removes x* from the relaxed bigm feasible region.

    Finds all the constraints which are tight at xhat (assumed to be the 
    solution currently in instance_rHull), and calculates a composite normal
    vector by summing the vectors normal to each of these constraints. Then
    Fourier-Motzkin elimination is used to project the disaggregated variables
    out of the polyhedron formed by the composite normal and the collection 
    of tight constraints. This results in multiple cuts, of which we select
    one which cuts of x* by the greatest margin, as long as that margin is
    more than cut_threshold. If no cut satisfies the margin specified by 
    cut_threshold, we return None.

    Parameters
    -----------
    var_info: 
    hull_to_bigm_map: 
    disaggregated_vars:
    disaggregation_constraints:
    rHull_vars:
    instance_rHull:
    rBigM_linear_constraints:
    transBlock_rBigm:
    transBlock_rHull:
    cut_threshold:
    """

    normal_vectors = []
    tight_constraints = Block()
    conslist = tight_constraints.constraints = Constraint(
        NonNegativeIntegers)
    conslist.construct()
    for constraint in instance_rHull.component_data_objects(
            Constraint,
            active=True,
            descend_into=Block,
            sort=SortComponents.deterministic):
        multiplier = _constraint_tight(instance_rHull, constraint)
        if multiplier:
            f = constraint.body
            firstDerivs = differentiate(f, wrt_list=rHull_vars)
            normal_vec = [multiplier*value(_) for _ in firstDerivs]
            normal_vectors.append(normal_vec)
            # check if constraint is linear
            if f.polynomial_degree() == 1:
                conslist[len(conslist)] = constraint.expr
            else: 
                # we will use the linear approximation of this constraint at
                # x_hat
                conslist[len(conslist)] = _get_linear_approximation_expr(
                    normal_vec, rHull_vars)
        # even if it was satisfied exactly, we need to grab the
        # disaggregation constraints in order to do the projection.
        elif constraint in disaggregation_constraints:
            conslist[len(conslist)] = constraint.expr

    # It is possible that the separation problem returned a point in
    # the interior of the convex hull.  It is also possible that the
    # only active constraints are (feasible) equality constraints.
    # in these situations, there are no normal vectors from which to
    # create a valid cut.
    if not normal_vectors:
        return None

    hull_xform = TransformationFactory('gdp.hull')

    composite_normal = list(
        sum(_) for _ in zip(*tuple(normal_vectors)) )
    composite_normal_map = ComponentMap(
        (v,n) for v,n in zip(rHull_vars, composite_normal))

    composite_cutexpr_Hull = 0
    for x_bigm, x_hull, x_star in var_info:
        # make the cut in the Hull space with the Hull variables. We will
        # translate it all to BigM and rBigM later when we have projected
        # out the disaggregated variables
        composite_cutexpr_Hull += composite_normal_map[x_hull]*\
                                   (x_hull - x_hull.value)

    # expand the composite_cutexprs to be in the extended space
    vars_to_eliminate = ComponentSet()
    do_fme = False
    # add the part of the expression involving the disaggregated variables.
    for x_disaggregated in disaggregated_vars:
        normal_vec_component = composite_normal_map[x_disaggregated]
        composite_cutexpr_Hull += normal_vec_component*\
                                   (x_disaggregated - x_disaggregated.value)
        vars_to_eliminate.add(x_disaggregated)
        # check that at least one disaggregated variable appears in the
        # constraint. Else we don't need to do FME
        if not do_fme and normal_vec_component != 0:
            do_fme = True

    conslist[len(conslist)] = composite_cutexpr_Hull <= 0

    if do_fme:
        tight_constraints.construct()
        TransformationFactory('contrib.fourier_motzkin_elimination').\
            apply_to(tight_constraints, vars_to_eliminate=vars_to_eliminate,
                     zero_tolerance=zero_tolerance)
        # I made this block, so I know they are here. Not that I won't hate
        # myself later for messing with private stuff.
        fme_results = tight_constraints._pyomo_contrib_fme_transformation.\
                      projected_constraints
        projected_constraints = [cons for i, cons in iteritems(fme_results)]
    else:
        # we didn't need to project, so it's the last guy we added.
        projected_constraints = [conslist[len(conslist) - 1]]

    # we created these constraints with the variables from rHull. We
    # actually need constraints for BigM and rBigM now!
    cuts = _get_constraint_exprs(projected_constraints, hull_to_bigm_map)

    # We likely have some cuts that duplicate other constraints now. We will
    # filter them to make sure that they do in fact cut off x*. If that's the
    # case, we know they are not already in the BigM relaxation. Because they
    # came from FME, they are very likely redundant, so we'll keep the best one
    # we find
    best = 0
    best_cut = None
    for i in sorted(range(len(cuts)), reverse=True):
        cut = cuts[i]
        # x* is still in rBigM, so we can just remove this constraint if it
        # is satisfied at x*
        logger.info("FME: Post-processing cut %s" % cut)
        if value(cut):
            logger.info("FME:\t Doesn't cut off x*")
            del cuts[i]
            continue
        # we have found a constraint which cuts of x* by some convincing
        # amount and is not already in rBigM, this has to be our cut and we
        # can stop. We know cut is lb <= expr and that it's violated
        assert len(cut.args) == 2
        cut_off = value(cut.args[0]) - value(cut.args[1])
        if cut_off > cut_threshold and cut_off > best:
            best = cut_off
            best_cut = cut
            logger.info("FME:\t New best cut: Cuts off x* by %s." % best)
    if best_cut is not None:
        return [best_cut]

    return None

def create_cuts_normal_vector(var_info, hull_to_bigm_map, disaggregated_vars,
                              disaggregation_constraints, rHull_vars,
                              instance_rHull, rBigM_linear_constraints,
                              transBlock_rBigM, transBlock_rHull, TOL,
                              zero_tolerance=None):
    cut_number = len(transBlock_rBigM.cuts)

    cutexpr = 0
    for x_rbigm, x_hull, x_star in var_info:
        cutexpr += (x_hull.value - x_star.value)*(x_rbigm - x_hull.value)

    # make sure we're cutting off x* by enough.
    if value(cutexpr) < -TOL:
        return [cutexpr >= 0]
    return None

def _restore_objective(instance_rHull, transBlock_rHull):
    transBlock_rHull.del_component(transBlock_rHull.infeasibility_objective)
    transBlock_rHull.separation_objective.activate()

def back_off_constraint_with_calculated_cut_violation(instance_rHull,
                                                      transBlock_rHull, cut,
                                                      bigm_to_hull_map, opt,
                                                      stream_solver, TOL):
    logger.info("Post-processing cut: %s" % cut.expr)
    # Take a constraint. We will solve a problem maximizing its violation
    # subject to rHull. We will add some user-specified tolerance to that
    # violation, and then add that much padding to it if it can be violated.
    transBlock_rHull.separation_objective.deactivate()

    transBlock_rHull.infeasibility_objective = Objective(
        expr=clone_without_expression_components(cut.body,
                                                 substitute=bigm_to_hull_map))

    results = opt.solve(instance_rHull, tee=stream_solver)
    if verify_successful_solve(results) is not NORMAL:
        logger.warning("Problem to determine how much to "
                       "back off the new cut "
                       "did not solve normally. Leaving the constraint as is, "
                       "which could lead to numerical trouble%s" % (results,))
        _restore_objective(instance_rHull, transBlock_rHull, orig_obj)
        return

    val = value(transBlock_rHull.infeasibility_objective) - TOL
    if val <= 0:
        logger.info("\tBacking off cut by %s" % val)
        cut._body += abs(val)
    # else there is nothing to do
    _restore_objective(instance_rHull, transBlock_rHull)

def back_off_constraint_by_fixed_tolerance(instance_rHull, transBlock_rHull,
                                           cut, bigm_to_hull_map, opt,
                                           stream_solver, TOL):
    cut._body += TOL

@TransformationFactory.register('gdp.cuttingplane',
                                doc="Relaxes a linear disjunctive model by "
                                "adding cuts from convex hull to Big-M "
                                "reformulation.")
class CuttingPlane_Transformation(Transformation):
    """Relax convex disjunctive model by forming the bigm relaxation and then
    iteratively adding cuts from the hull relaxation (or the hull relaxation
    after some basic steps) in order to strengthen the formulation.

    This transformation accepts the following keyword arguments:
    
    Parameters
    ----------
    solver : Solver name (as string) to use to solve relaxed BigM and separation
             problems
    solver_options : dictionary of options to pass to the solver
    stream_solver : Whether or not to display solver output
    verbose : Enable verbose output from cuttingplanes algorithm
    minimum_improvement_threshold : Minimum difference in relaxed BigM objective
                                    values between consecutive iterations
    tighten_relaxation : callback to modify the GDP model before the hull 
                         relaxation is taken (e.g. could be used to perform 
                         basic steps)
    create_cuts : callback to create cuts using the solved relaxed bigM and hull
                  problems
    post_process_cut : callback to perform post-processing on created cuts
    back_off_problem_tolerance : tolerance to use while post-processing
    cut_threshold : Amount by which cut is violated at the relaxed bigM
                    solution in order to be added to the bigM model
    zero_tolerance : Tolerance at which a float will be considered 0 when
                     using Fourier-Motzkin elimination to create cuts.

    By default, the callbacks will be set such that the algorithm performed is
    as presented in [1], but with an additional post-processing procedure to
    reduce numerical error, which calculates the maximum violation of the cut 
    subject to the relaxed hull constraints, and then pads the constraint by 
    this violation plus an additional user-specified tolerance.

    In addition, the create_cuts_fme function provides an (exponential time)
    method of generating cuts which reduces numerical error (and can eliminate 
    it if all data is integer). It collects the hull constraints which are 
    tight at the solution of the separation problem. It creates a cut in the 
    extended space perpendicular to  a composite normal vector created by 
    summing the directions normal to these constraints. It then performs 
    fourier-motzkin elimination on the collection of constraints and the cut
    to project out the disaggregated variables. The resulting constraint which
    is most violated by the relaxed bigM solution is then returned.

    References
    ----------
        [1] Sawaya, N. W., Grossmann, I. E. (2005). A cutting plane method for 
        solving linear generalized disjunctive programming problems. Computers
        and Chemical Engineering, 29, 1891-1913 
    """

    CONFIG = ConfigBlock("gdp.cuttingplane")
    CONFIG.declare('solver', ConfigValue(
        default='ipopt',
        domain=str,
        description="""Solver to use for relaxed BigM problem and the separation
        problem""",
        doc="""
        This specifies the solver which will be used to solve LP relaxation
        of the BigM problem and the separation problem. Note that this solver
        must be able to handle a quadratic objective because of the separation
        problem.
        """
    ))
    CONFIG.declare('minimum_improvement_threshold', ConfigValue(
        default=0.01,
        domain=PositiveFloat,
        description="Threshold value used to decide when to stop adding cuts",
        doc="""
        If the difference between the objectives in two consecutive iterations is
        less than this value, the algorithm terminates without adding the cut
        generated in the last iteration.  
        """
    ))
    CONFIG.declare('verbose', ConfigValue(
        default=False,
        domain=bool,
        description="Flag to enable verbose output",
        doc="""
        If True, prints subproblem solutions, as well as potential and added cuts
        during algorithm.

        If False, only the relaxed BigM objective and minimal information about 
        cuts is logged.
        """
    ))
    CONFIG.declare('stream_solver', ConfigValue(
        default=False,
        domain=bool,
        description="""If true, sets tee=True for every solve performed over
        "the course of the algorithm"""
    ))
    # TODO: I'd rather just have them pass the SolverFactory, if we can make
    # that possible...
    CONFIG.declare('solver_options', ConfigValue(
        default={},
        description="Dictionary of solver options",
        doc="""
        Dictionary of solver options that will be set for the solver for both the
        relaxed BigM and separation problem solves.
        """
    ))
    CONFIG.declare('tighten_relaxation', ConfigValue(
        default=do_not_tighten,
        description="Callback which takes the GDP formulation and returns a "
        "GDP formulation with a tighter hull relaxation",
        doc="""
        Function which accepts the GDP formulation of the problem and returns
        a GDP formulation which the transformation will then take the hull
        reformulation of.

        Most typically, this callback would be used to apply basic steps before
        taking the hull reformulation, but anything which tightens the GDP can 
        be performed here.
        """
    ))
    CONFIG.declare('create_cuts', ConfigValue(
        default=create_cuts_normal_vector,
        description="Callback which generates a list of cuts, given the solved "
        "relaxed bigM and relaxed hull solutions. If no cuts can be "
        "generated, returns None",
        doc="""
        TODO
        """
    ))
    CONFIG.declare('post_process_cut', ConfigValue(
        default=back_off_constraint_with_calculated_cut_violation,
        description="Callback which takes a generated cut and post processes "
        "it, presumably to back it off in the case of numerical error. Set to "
        "None if not post-processing is desired.",
        doc="""
        TODO
        """
    ))
    # back off problem tolerance (on top of the solver's (sometimes))
    CONFIG.declare('back_off_problem_tolerance', ConfigValue(
        default=1e-8,
        domain=NonNegativeFloat,
        description="Tolerance to pass to the post_process_cut callback.",
        doc="""
        Tolerance passed to the post_process_cut callback.

        Depending on the callback, different values could make sense, but 
        something on the order of the solver's optimality or constraint 
        tolerances is appropriate.
        """
    ))
    CONFIG.declare('cut_filtering_threshold', ConfigValue(
        default=0.001,
        domain=NonNegativeFloat,
        description="Tolerance used to decide if a cut removes x* from the "
        "relaxed BigM problem by enough to be added to the bigM problem.",
        doc="""
        Absolute tolerance used to decide whether to keep a cut. We require
        that, when evaluated at x* (the relaxed BigM optimal solution), the 
        cut be infeasible by at least this tolerance.
        """
    ))
    CONFIG.declare('zero_tolerance', ConfigValue(
        default=1e-9,
        domain=NonNegativeFloat,
        description="Tolerance at which floats are assumed to be 0 while "
        "performing Fourier-Motzkin elimination",
        doc="""
        Only relevant when create_cuts=create_cuts_fme, this sets the 
        zero_tolerance option for the Fourier-Motzkin elimination transformation.
        """
    ))

    def __init__(self):
        super(CuttingPlane_Transformation, self).__init__()

    def _apply_to(self, instance, bigM=None, **kwds):
        assert not NAME_BUFFER
        self._config = self.CONFIG(kwds.pop('options', {}))
        self._config.set_value(kwds)

        if self._config.verbose:
            logger.setLevel(logging.INFO)

        (instance_rBigM, instance_rHull, var_info, 
         disaggregated_vars, disaggregation_constraints, 
         rBigM_linear_constraints, transBlockName) = self._setup_subproblems(
             instance, bigM, self._config.tighten_relaxation)

        self._generate_cuttingplanes( instance_rBigM, instance_rHull, var_info,
                                      disaggregated_vars,
                                      disaggregation_constraints,
                                      rBigM_linear_constraints, transBlockName)

        # restore integrality
        TransformationFactory('core.relax_integer_vars').apply_to(instance,
                                                                  undo=True)

        # clear the global name buffer
        NAME_BUFFER.clear()

    def _setup_subproblems(self, instance, bigM, tighten_relaxation_callback):
        # create transformation block
        transBlockName, transBlock = self._add_relaxation_block(
            instance,
            '_pyomo_gdp_cuttingplane_relaxation')

        # We store a list of all vars so that we can efficiently
        # generate maps among the subproblems

        # TODO: In the other transformations, we are able to offer an option
        # about how to handle fixed variables. We don't have the same luxury
        # here unless we unfix them and then fix them back at the end. Should we
        # do that?
        transBlock.all_vars = list(v for v in instance.component_data_objects(
            Var,
            descend_into=(Block, Disjunct),
            sort=SortComponents.deterministic) if not v.is_fixed())

        # we'll store all the cuts we add together
        transBlock.cuts = Constraint(NonNegativeIntegers)

        # get bigM and hull relaxations
        bigMRelaxation = TransformationFactory('gdp.bigm')
        hullRelaxation = TransformationFactory('gdp.hull')
        relaxIntegrality = TransformationFactory('core.relax_integer_vars')

        #
        # Generate the Hull relaxation (used for the separation
        # problem to generate cutting planes)
        #
        tighter_instance = tighten_relaxation_callback(instance)
        instance_rHull = hullRelaxation.create_using(instance)
        # collect a list of disaggregated variables.
        (disaggregated_vars, 
         disaggregation_constraints) = self._get_disaggregated_vars(
             instance_rHull)
        relaxIntegrality.apply_to(instance_rHull,
                                  transform_deactivated_blocks=True)

        #
        # Reformulate the instance using the BigM relaxation (this will
        # be the final instance returned to the user)
        #
        bigMRelaxation.apply_to(instance, bigM=bigM)

        #
        # Generate the continuous relaxation of the BigM transformation. We'll
        # restore it at the end.
        #
        relaxIntegrality.apply_to(instance, transform_deactivated_blocks=True)

        fme = TransformationFactory('contrib.fourier_motzkin_elimination')
        #
        # Collect all of the linear constraints that are in the rBigM
        # instance. We will need these so that we can compare what we get from
        # FME to them and make sure we aren't adding redundant constraints to
        # the model. For convenience, we will make sure they are all in the form
        # lb <= expr (so we will break equality constraints)
        #
        rBigM_linear_constraints = []
        for cons in instance.component_data_objects(
                Constraint,
                descend_into=Block,
                sort=SortComponents.deterministic,
                active=True):
            body = cons.body
            if body.polynomial_degree() != 1:
                # We will never get a nonlinear constraint out of FME, so we
                # don't risk it being identical to this one.
                continue

            # TODO: Guess this shouldn't have been private...
            rBigM_linear_constraints.extend(fme._process_constraint(cons))

        # [ESJ Aug 13 2020] NOTE: We actually don't need to worry about variable
        # bounds here becuase the FME transformation will take care of them
        # (i.e. convert them to constraints for the purposes of the projection.)

        #
        # Add the xstar parameter for the Hull problem
        #
        transBlock_rHull = instance_rHull.component(transBlockName)
        #
        # this will hold the solution to rbigm each time we solve it. We
        # add it to the transformation block so that we don't have to
        # worry about name conflicts.
        transBlock_rHull.xstar = Param( range(len(transBlock.all_vars)),
                                        mutable=True, default=None,
                                        within=[None] | Reals)
        # we will add a block that we will deactivate to use to store the
        # extended space cuts. We never need to solve these, but we need them to
        # be contructed for the sake of Fourier-Motzkin Elimination
        extendedSpaceCuts = transBlock_rHull.extendedSpaceCuts = Block()
        extendedSpaceCuts.deactivate()
        extendedSpaceCuts.cuts = Constraint(Any)

        #
        # Generate the mapping between the variables on all the
        # instances and the xstar parameter.
        #
        var_info = tuple(
            (v, # this is the bigM variable
             transBlock_rHull.all_vars[i],
             transBlock_rHull.xstar[i])
            for i,v in enumerate(transBlock.all_vars))

        #
        # Add the separation objective to the hull subproblem
        #
        self._add_separation_objective(var_info, transBlock_rHull)

        return (instance, instance_rHull, var_info, disaggregated_vars,
                disaggregation_constraints, rBigM_linear_constraints,
                transBlockName)

    # this is the map that I need to translate my projected cuts and add
    # them to bigM
    def _create_hull_to_bigm_substitution_map(self, var_info):
        return dict((id(var_info[i][1]), var_info[i][0]) for i in
                    range(len(var_info)))

    # this map is needed to solve the back-off problem for post-processing
    def _create_bigm_to_hull_substition_map(self, var_info):
        return dict((id(var_info[i][0]), var_info[i][1]) for i in
                    range(len(var_info)))

    def _get_disaggregated_vars(self, hull):
        disaggregatedVars = []
        disaggregationConstraints = ComponentSet()
        hull_xform = TransformationFactory('gdp.hull')
        for disjunction in hull.component_data_objects( Disjunction,
                                                        descend_into=(Disjunct,
                                                                      Block)):
            for disjunct in disjunction.disjuncts:
                if disjunct.transformation_block is not None:
                    transBlock = disjunct.transformation_block()
                    for v in transBlock.disaggregatedVars.\
                        component_data_objects(Var):
                        disaggregatedVars.append(v)
                        disaggregationConstraints.add(
                            hull_xform.get_disaggregation_constraint(
                                hull_xform.get_src_var(v), disjunction))
                
        return disaggregatedVars, disaggregationConstraints

    def _generate_cuttingplanes( self, instance_rBigM, instance_rHull, var_info,
                                 disaggregated_vars, disaggregation_constraints,
                                 rBigM_linear_constraints, transBlockName):

        opt = SolverFactory(self._config.solver)
        stream_solver = self._config.stream_solver
        opt.options = self._config.solver_options

        improving = True
        prev_obj = float("inf")
        epsilon = self._config.minimum_improvement_threshold
        cuts = None

        transBlock_rBigM = instance_rBigM.component(transBlockName)
        transBlock_rHull = instance_rHull.component(transBlockName)

        # We try to grab the first active objective. If there is more
        # than one, the writer will yell when we try to solve below. If
        # there are 0, we will yell here.
        rBigM_obj = next(instance_rBigM.component_data_objects(
            Objective, active=True), None)
        if rBigM_obj is None:
            raise GDP_Error("Cannot apply cutting planes transformation "
                            "without an active objective in the model!")

        # Get list of all variables in the rHull model which we will use when
        # calculating the composite normal vector.
        rHull_vars = [i for i in instance_rHull.component_data_objects(
            Var,
            descend_into=Block,
            sort=SortComponents.deterministic)]

        hull_to_bigm_map = self._create_hull_to_bigm_substitution_map(var_info)
        bigm_to_hull_map = self._create_bigm_to_hull_substition_map(var_info)

        while (improving):
            # solve rBigM, solution is xstar
            results = opt.solve(instance_rBigM, tee=stream_solver)
            if verify_successful_solve(results) is not NORMAL:
                logger.warning("Relaxed BigM subproblem "
                               "did not solve normally. Stopping cutting "
                               "plane generation.\n\n%s" % (results,))
                return

            rBigM_objVal = value(rBigM_obj)
            logger.warning("rBigM objective = %s"
                           % (rBigM_objVal,))

            # copy over xstar
            logger.info("x* is:")
            for x_rbigm, x_hull, x_star in var_info:
                x_star.value = x_rbigm.value
                # initialize the X values
                x_hull.value = x_rbigm.value
                logger.info("\t%s = %s" % 
                            (x_rbigm.getname(fully_qualified=True,
                                             name_buffer=NAME_BUFFER),
                             x_rbigm.value))

            # compare objectives: check absolute difference close to 0, relative
            # difference further from 0.
            obj_diff = prev_obj - rBigM_objVal
            improving = math.isinf(obj_diff) or \
                        ( abs(obj_diff) > epsilon if abs(rBigM_objVal) < 1 else
                          abs(obj_diff/prev_obj) > epsilon )

            # solve separation problem to get xhat.
            opt.solve(instance_rHull, tee=stream_solver)
            if verify_successful_solve(results) is not NORMAL:
                logger.warning("Hull separation subproblem "
                               "did not solve normally. Stopping cutting "
                               "plane generation.\n\n%s" % (results,))
                return
            logger.info("xhat is: ")
            for x_rbigm, x_hull, x_star in var_info:
                logger.info("\t%s = %s" % 
                            (x_hull.getname(fully_qualified=True,
                                            name_buffer=NAME_BUFFER), 
                             x_hull.value))

            # [JDS 19 Dec 18] Note: we check that the separation objective was
            # significantly nonzero.  If it is too close to zero, either the
            # rBigM solution was in the convex hull, or the separation vector is
            # so close to zero that the resulting cut is likely to have
            # numerical issues.
            if abs(value(transBlock_rHull.separation_objective)) < epsilon:
                break

            cuts = self._config.create_cuts(var_info, hull_to_bigm_map,
                                            disaggregated_vars,
                                            disaggregation_constraints,
                                            rHull_vars, instance_rHull,
                                            rBigM_linear_constraints,
                                            transBlock_rBigM, transBlock_rHull,
                                            self._config.cut_filtering_threshold,
                                            self._config.zero_tolerance)
           
            # We are done if the cut generator couldn't return a valid cut
            if cuts is None or not improving:
                break

            for cut in cuts:
                cut_number = len(transBlock_rBigM.cuts)
                logger.warning("Adding cut %s to BigM model." % (cut_number,))
                transBlock_rBigM.cuts.add(cut_number, cut)
                if self._config.post_process_cut is not None:
                    self._config.post_process_cut( 
                        instance_rHull,
                        transBlock_rHull,
                        transBlock_rBigM.cuts[cut_number],
                        bigm_to_hull_map, opt,
                        stream_solver,
                        self._config.back_off_problem_tolerance)

            prev_obj = rBigM_objVal


    def _add_relaxation_block(self, instance, name):
        # creates transformation block with a unique name based on name, adds it
        # to instance, and returns it.
        transBlockName = unique_component_name(
            instance,
            '_pyomo_gdp_cuttingplane_transformation')
        transBlock = Block()
        instance.add_component(transBlockName, transBlock)
        return transBlockName, transBlock


    def _add_separation_objective(self, var_info, transBlock_rHull):
        # Deactivate any/all other objectives
        for o in transBlock_rHull.model().component_data_objects(Objective):
            o.deactivate()

        obj_expr = 0
        for x_rbigm, x_hull, x_star in var_info:
            obj_expr += (x_hull - x_star)**2
        # add separation objective to transformation block
        transBlock_rHull.separation_objective = Objective(expr=obj_expr)

        


