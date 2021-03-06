# -*- coding: utf-8 -*-
"""Simulation utils."""
# pylint: disable=no-member,too-many-arguments

import logging

import parmed
import simtk.openmm as mm
import simtk.openmm.app as mmapp
from simtk import unit
from simtk.openmm import XmlSerializer
from simtk.openmm.openmm import LangevinIntegrator, State, VerletIntegrator

import mmlite.defaults
from mmlite import SEED

from .output import add_screen_output, add_state_output, add_trajectory_output

logger = logging.getLogger(__name__)


# pylint: disable=too-many-instance-attributes
class Simulation(mmapp.Simulation):
    """
    Simulation class.

    Main attributes:
    * integrator
    * context

    Main methods:
    * minimize
    * step
    * saveCheckpoint
    * loadCheckpoint
    * saveState
    * loadState

    """
    def __init__(
            self,  # pylint: disable=super-init-not-called
            sys,
            integrator=None,
            platform=None,
            platform_properties=None,
            state=None):

        self.sys = sys
        super().__init__(self.sys.topology,
                         self.sys.system,
                         integrator,
                         platform=platform,
                         platformProperties=platform_properties,
                         state=state)

        self._state = None
        self._integrator = None  # current integrator
        # Initialize a context containing the current state of the simulation
        # If state is passed, initialize from state
        self.setup_context(xp=self.sys.positions,
                           platform=platform,
                           properties=platform_properties,
                           state=state)
        self.currentStep = 0  # Current time step

        # Initialize a list of reporters to invoke during the simulation
        self.reporters = []
        # add reporters
        add_trajectory_output(self)
        add_screen_output(self)
        add_state_output(self)

        try:
            self._usesPBC = self.system.usesPeriodicBoundaryConditions()
            # OpenMM just raises Exception if it's not implemented everywhere
        except Exception:  # pylint: disable=broad-except
            self._usesPBC = self.topology.getUnitCellDimensions() is not None

    def setup_context(self,
                      xp=None,
                      temperature=mmlite.defaults.temperature,
                      platform=None,
                      properties=None,
                      state=None):
        """Init an integrator and return a fresh context."""
        # We have to create a new integrator for every Context since it takes
        # ownership of the integrator we pass it
        args = [self.sys.system, self.integrator]
        if platform is not None:
            platform = mm.Platform.getPlatformByName(platform)
            args.append(platform)
        if properties is not None:
            args.append(properties)
        context = mm.Context(*args)
        if xp is not None:
            context.setPositions(xp)
        if temperature:
            context.setVelocitiesToTemperature(temperature, SEED)
        # Initialize from state
        if state is not None:
            with open(state, 'r') as f:
                self.context.setState(mm.XmlSerializer.deserialize(f.read()))

        self.context = context
        return context

    @property
    def integrator(self):
        """The actual integrator."""
        if self._integrator is None:
            # set to the default integrator
            self._integrator = self.create_integrator()
        return self._integrator

    @integrator.setter
    def integrator(self, value):
        self._integrator = value

    @staticmethod
    def create_integrator(name='verlet', dt=1 * unit.femtoseconds, **kwargs):
        """Return a fresh integrator."""
        if name == 'verlet':
            return VerletIntegrator(dt)
        if name == 'langevin':
            temperature = kwargs.pop('temperature',
                                     mmlite.defaults.temperature)
            friction = kwargs.pop('friction', mmlite.defaults.friction)
            return LangevinIntegrator(temperature, friction, dt)
        raise ValueError(name)

    def serialize(self):
        """Return the System and positions in serialized XML form.

        Returns
        -------

        system_xml : str
            Serialized XML form of System object.

        state_xml : str
            Serialized XML form of State object containing particle positions.

        """

        # Serialize System.
        system_xml = XmlSerializer.serialize(self._system)

        # Serialize positions via State.
        if self._system.getNumParticles() == 0:
            # Cannot serialize the State of a system with no particles.
            state_xml = None
        else:
            platform = mm.Platform.getPlatformByName('Reference')
            integrator = mm.VerletIntegrator(1.0 * unit.femtoseconds)
            context = mm.Context(self._system, integrator, platform)
            context.setPositions(self.positions)
            state = context.getState(getPositions=True)  # pylint: disable=unexpected-keyword-arg, no-value-for-parameter
            del context, integrator
            state_xml = XmlSerializer.serialize(state)

        return (system_xml, state_xml)

    def minimize(self, tol=10 * unit.kilojoule / unit.mole, max_iter=0):
        """Perform a local energy minimization on the system.

        Parameters
        ----------
        simulation : Simulation or Context object.
        tol : energy=10*kilojoules/mole
            The energy tolerance to which the system should be minimized
        max_iter : int=None
            The maximum number of iterations to perform.  If this is 0,
            Default: minimization is continued until the results converge.

        Returns
        -------
        Context or Simulation.

        """
        logger.info('Energy before minimization: %s',
                    simulation_energy(self.context)['potential'])

        self.minimizeEnergy(tolerance=tol, maxIterations=max_iter)

        logger.info('Energy after minimization: %s',
                    simulation_energy(self.context)['potential'])

    @property
    def positions(self):
        """Actual positions."""
        state = simulation_state(self, data='positions')
        return state_data(state, data='positions')

    @positions.setter
    def positions(self, value):
        self.context.setPositions(value)

    def update_state(self, data='positions'):
        """Update simulation state."""
        self._state = simulation_state(self, data=data)

    def get_state(self, data='positions'):
        """Return simulation state."""
        if self._state is None:
            self.update_state(data)
        return self._state

    state = property(get_state)

    @state.setter
    def state(self, value):
        self._state = value
        self.context.setState(value)

    def data(self, qs):
        """Return quantities from simulation state."""
        return state_data(self.state, qs)


def camelcase(a):
    """Convert string to camelcase."""
    return a.title().replace('_', '')


def state_property(state, property_name):
    """Return the value of a state property from quantity_name.

    Parameters
    ----------
    state : State object
    property_name : str or array-like
        Property name (all-lowercase, underscore separated format)
        If a list is passed, a list of values will be returned. If `name`
        contains whitespaces, split it into a list of names.

    Returns
    -------
    value/list of values.

    """
    if isinstance(property_name, str) and len(property_name.split()) == 1:
        method_name = 'get' + camelcase(property_name)
        method = getattr(state, method_name)
        kwargs = {}
        if property_name in 'forces periodic_box_vectors positions velocities':
            kwargs['asNumpy'] = True
        return method(**kwargs)

    if isinstance(property_name, str):
        property_name = property_name.split()
    return [state_property(state, q) for q in property_name]


def state_data(state, data='positions'):
    """
    Return data from context state.

    Default: 'positions'.

    """

    if isinstance(data, str):
        data = data.split()

    result = []
    for q in data:
        result.append(state_property(state, q))
    return result if len(data) > 1 else result[0]


def simulation_state(simulation, data='positions', pbc=False, groups=-1):
    """
    Return a context state containing the quantities defined in `data`.

    Parameters
    ----------
    simulation : Simulation or Context or State object.
    data : list or str
        List of quantities to include in the context state.
        If a string, split into a list.
        Valid values are: {'positions', 'velocities', 'forces', 'energy',
        'parameters', 'parameter_derivatives'}
    pbc : bool=False
        Center molecules in the same cell.
    groups : set=set(range(32))
        Set of force groups indices to include when computing forces and
        energies. Default: include all energies.

    Returns
    -------
    state object

    """
    if isinstance(simulation, State):  # if a State object, just return
        return simulation

    try:
        context = simulation.context
    except AttributeError:
        context = simulation

    if isinstance(data, str):
        data = data.split()

    if data:
        data = {'get' + camelcase(a): True for a in data}
    else:
        data = {}

    return context.getState(**data, enforcePeriodicBox=pbc, groups=groups)


def context_state(context, data='positions', pbc=True, groups=-1):
    """
    Return a context state containing the quantities defined in `data`.

    Parameters
    ----------
    context : Context or State object.
    data : list or str, optional
        List of quantities to include in the context state.
        If a string, split into a list. Default: 'positions'.
        Valid values are: {'positions', 'velocities', 'forces', 'energy',
        'parameters', 'parameter_derivatives'}
    pbc : bool=False
        Center molecules in the same cell.
    groups : set=set(range(32))
        Set of force groups indices to include when computing forces and
        energies. Default: include all energies.

    Returns
    -------
    state object

    """
    if isinstance(context, State):  # if a State object, just return
        return context

    if isinstance(data, str):
        data = data.split()

    if data:
        data = {'get' + camelcase(a): True for a in data}
    else:
        data = {}

    return context.getState(**data, enforcePeriodicBox=pbc, groups=groups)


def simulation_data(simulation, data=None, pbc=False, groups=-1):
    """
    Return data from simulation state.

    Parameters
    ----------
    simulation : Simulation or Context object.
    data : list or str
        List of quantities to include in the context state.
        If a string, split into a list.
        Valid values are: {positions, velocities, forces, energy, parameters}
    pbc : bool=False
        Center molecules in the same cell.
    groups : set=set(range(32))
        Set of force groups indices to include when computing forces and
        energies. Default: include all energies.

    Returns
    -------
    dict
        A dict containing the potential and kinetic energy

    """

    # state = simulation_state(simulation, data=data, pbc=pbc, groups=-1)

    raise NotImplementedError


def simulation_energy(simulation):
    """
    Return the potential and kinetic energy.

    Parameters
    ----------
    simulation : Simulation or Context object.

    Returns
    -------
    dict
        A dict containing the potential and kinetic energy

    """

    state = simulation_state(simulation, 'energy')

    return {
        'potential': state.getPotentialEnergy(),
        'kinetic': state.getKineticEnergy()
    }


def simulation_positions(simulation):
    """
    Return atomic coordinates.

    Parameters
    ----------
    simulation : Simulation or Context object.

    Returns
    -------
    ndarray

    """

    state = simulation_state(simulation, 'positions')

    return state.getPositions(asNumpy=True)


def simulation_forces(simulation):
    """
    Return atomic forces.

    Parameters
    ----------
    simulation : Simulation or Context object.

    Returns
    -------
    ndarray

    """

    state = simulation_state(simulation, 'forces')

    return state.getForces(asNumpy=True)


def simulation_velocities(simulation):
    """
    Return atomic velocities.

    Parameters
    ----------
    simulation : Simulation or Context object.

    Returns
    -------
    ndarray

    """

    state = simulation_state(simulation, 'velocities')

    return state.getVelocities(asNumpy=True)


def set_simulation_temperature(simulation, temperature=298):
    """Initialize velocities according to temperature `t`."""
    try:
        context = simulation.context
    except AttributeError:
        context = simulation

    context.setVelocitiesToTemperature(temperature, SEED)


def set_simulation_positions(simulation, xp):
    """Set positions to `xp`."""
    try:
        context = simulation.context
    except AttributeError:
        context = simulation
    context.setPositions(xp)


def simulation_structure(simulation, velocities=False):
    """
    Extract a parmed Structure object from `simulation`.

    Parameters
    ----------
    simulation : openmm.app.Simulation
        OpenMM Simulation object.
    velocities : bool, optional
        Store velocities, defaults to False.

    Returns
    -------
    parmed.Structure object.

    """

    data = ['positions']
    if velocities:
        data.append('velocities')

    state = simulation_state(simulation, data=data, pbc=True)
    positions, velocities = state_data(state, data=data)
    structure = parmed.openmm.load_topology(simulation.topology,
                                            simulation.system)
    structure.positions = positions
    structure.velocities = velocities
    return structure
