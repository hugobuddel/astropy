# -*- coding: utf-8 -*-
# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module defines the `Quantity` object, which represents a number with some
associated units. `Quantity` objects support operations like ordinary numbers,
but will deal with unit conversions internally.
"""

from __future__ import (absolute_import, unicode_literals, division,
                        print_function)

# Standard library
import numbers

import numpy as np
NUMPY_LT_1P9 = [int(x) for x in np.__version__.split('.')[:2]] < [1, 9]

# AstroPy
from ..extern import six
from .core import (Unit, dimensionless_unscaled, UnitBase, UnitsError,
                   get_current_unit_registry)
from ..utils import lazyproperty
from ..utils.compat.misc import override__dir__
from ..utils.misc import isiterable, InheritDocstrings
from .utils import validate_power


__all__ = ["Quantity"]


# We don't want to run doctests in the docstrings we inherit from Numpy
__doctest_skip__ = ['Quantity.*']


def _can_cast(arg, dtype):
    """
    This is needed for compatibility with Numpy < 1.6, in which ``can_cast``
    can only take a dtype or type as its first argument.
    """
    return np.can_cast(getattr(arg, 'dtype', type(arg)), dtype)


_UNIT_NOT_INITIALISED = "(Unit not initialised)"


def _can_have_arbitrary_unit(value):
    """Test whether the items in value can have arbitrary units

    Numbers whose value does not change upon a unit change, i.e.,
    zero, infinity, or not-a-number

    Parameters
    ----------
    value : number or array

    Returns
    -------
    `True` if each member is either zero or not finite, `False` otherwise
    """
    return np.all(np.logical_or(np.equal(value, 0.), ~np.isfinite(value)))


class QuantityIterator(object):
    """
    Flat iterator object to iterate over Quantities

    A `QuantityIterator` iterator is returned by ``q.flat`` for any Quantity
    ``q``.  It allows iterating over the array as if it were a 1-D array,
    either in a for-loop or by calling its `next` method.

    Iteration is done in C-contiguous style, with the last index varying the
    fastest. The iterator can also be indexed using basic slicing or
    advanced indexing.

    See Also
    --------
    Quantity.flatten : Returns a flattened copy of an array.

    Notes
    -----
    `QuantityIterator` is inspired by `~numpy.ma.core.MaskedIterator`.  It
    is not exported by the `~astropy.units` module.  Instead of
    instantiating a `QuantityIterator` directly, use `Quantity.flat`.
    """
    def __init__(self, q):
        self._quantity = q
        self._dataiter = q.view(np.ndarray).flat

    def __iter__(self):
        return self

    def __getitem__(self, indx):
        out = self._dataiter.__getitem__(indx)
        return self._quantity._new_view(out)

    def __setitem__(self, index, value):
        self._dataiter[index] = self._quantity._to_own_unit(value)

    def __next__(self):
        """
        Return the next value, or raise StopIteration.
        """
        out = next(self._dataiter)
        return self._quantity._new_view(out)

    next = __next__


@six.add_metaclass(InheritDocstrings)
class Quantity(np.ndarray):
    """ A `Quantity` represents a number with some associated unit.

    Parameters
    ----------
    value : number, `~numpy.ndarray`, `Quantity` object, or sequence of `Quantity` objects.
        The numerical value of this quantity in the units given by unit.  If a
        `Quantity` or sequence of them (or any other valid object with a
        ``unit`` attribute), creates a new `Quantity` object, converting to
        `unit` units as needed.

    unit : `~astropy.units.UnitBase` instance, str
        An object that represents the unit associated with the input value.
        Must be an `~astropy.units.UnitBase` object or a string parseable by
        the :mod:`~astropy.units` package.

    dtype : ~numpy.dtype, optional
        The dtype of the resulting Numpy array or scalar that will
        hold the value.  If not provided, it is determined from the input,
        except that any input that cannot represent float (integer and bool)
        is converted to float.

    copy : bool, optional
        If `True` (default), then the value is copied.  Otherwise, a copy will
        only be made if ``__array__`` returns a copy, if value is a nested
        sequence, or if a copy is needed to satisfy an explicitly given
        ``dtype``.  (The `False` option is intended mostly for internal use,
        to speed up initialization where a copy is known to have been made.
        Use with care.)

    order : {'C', 'F', 'A'}, optional
        Specify the order of the array.  As in `~numpy.array`.  This parameter
        is ignored if the input is a `Quantity` and ``copy=False``.

    subok : bool, optional
        If `False` (default), the returned array will be forced to be a
        `Quantity`.  Otherwise, `Quantity` subclasses will be passed through.

    ndmin : int, optional
        Specifies the minimum number of dimensions that the resulting array
        should have.  Ones will be pre-pended to the shape as needed to meet
        this requirement.  This parameter is ignored if the input is a
        `Quantity` and ``copy=False``.

    Raises
    ------
    TypeError
        If the value provided is not a Python numeric type.
    TypeError
        If the unit provided is not either a :class:`~astropy.units.Unit`
        object or a parseable string unit.
    """
    # Need to set a class-level default for _equivalencies, or
    # Constants can not initialize properly
    _equivalencies = []

    __array_priority__ = 10000

    def __new__(cls, value, unit=None, dtype=None, copy=True, order=None,
                subok=False, ndmin=0):

        if unit is not None:
            # convert unit first, to avoid multiple string->unit conversions
            unit = Unit(unit)

        # optimize speed for Quantity with no dtype given, copy=False
        if isinstance(value, Quantity):
            if unit is not None and unit is not value.unit:
                value = value.to(unit)
                # the above already makes a copy (with float dtype)
                copy = False

            if not subok and type(value) is not cls:
                value = value.view(cls)

            if dtype is None:
                if not copy:
                    return value

                if not np.can_cast(np.float32, value.dtype):
                    dtype = np.float

            return np.array(value, dtype=dtype, copy=copy, order=order,
                            subok=True, ndmin=ndmin)

        rescale_value = None

        # Maybe list/tuple of Quantity? short-circuit array for speed
        if(not isinstance(value, np.ndarray) and isiterable(value) and
           all(isinstance(v, Quantity) for v in value)):
            if unit is None:
                unit = value[0].unit
            value = [q.to(unit).value for q in value]
            copy = False  # copy already made

        else:
            # if the value has a `unit` attribute, treat it like a quantity by
            # rescaling the value appropriately
            if hasattr(value, 'unit'):
                try:
                    value_unit = Unit(value.unit)
                except TypeError:
                    if unit is None:
                        unit = dimensionless_unscaled
                else:
                    if unit is None:
                        unit = value_unit
                    else:
                        rescale_value = value_unit.to(unit)

            #if it has no unit, default to dimensionless_unscaled
            elif unit is None:
                unit = dimensionless_unscaled

        value = np.array(value, dtype=dtype, copy=copy, order=order,
                         subok=False, ndmin=ndmin)

        # check that array contains numbers or long int objects
        if (value.dtype.kind in 'OSU' and
            not (value.dtype.kind == 'O' and
                 isinstance(value.item(() if value.ndim == 0 else 0),
                            numbers.Number))):
            raise TypeError("The value must be a valid Python or "
                            "Numpy numeric type.")

        # by default, cast any integer, boolean, etc., to float
        if dtype is None and (not np.can_cast(np.float32, value.dtype)
                              or value.dtype.kind == 'O'):
            value = value.astype(np.float)

        if rescale_value is not None:
            value *= rescale_value

        value = value.view(cls)
        value._unit = unit

        return value

    def __array_finalize__(self, obj):
        self._unit = getattr(obj, '_unit', None)

    def __array_prepare__(self, obj, context=None):
        # This method gets called by Numpy whenever a ufunc is called on the
        # array. The object passed in ``obj`` is an empty version of the
        # output array which we can e.g. change to an array sub-class, add
        # attributes to, etc. After this is called, then the ufunc is called
        # and the values in this empty array are set.

        # If no context is set, just return the input
        if context is None:
            return obj

        # Find out which ufunc is being used
        function = context[0]

        from .quantity_helper import UNSUPPORTED_UFUNCS, UFUNC_HELPERS

        # Check whether we even support this ufunc
        if function in UNSUPPORTED_UFUNCS:
            raise TypeError("Cannot use function '{0}' with quantities"
                            .format(function.__name__))

        # Now find out what arguments were passed to the ufunc, usually, this
        # will include at least the present object, and another, which could
        # be a Quantity, or a Numpy array, etc. when using two-argument ufuncs.
        args = context[1][:function.nin]
        units = [getattr(arg, 'unit', None) for arg in args]

        # If the ufunc is supported, then we call a helper function (defined
        # in quantity_helper.py) which returns the scale by which the inputs
        # should be multiplied before being passed to the ufunc, as well as
        # the unit the output from the ufunc will have.
        if function in UFUNC_HELPERS:
            scales, result_unit = UFUNC_HELPERS[function](function, *units)
        else:
            raise TypeError("Unknown ufunc {0}.  Please raise issue on "
                            "https://github.com/astropy/astropy"
                            .format(function.__name__))

        if any(scale == 0. for scale in scales):
            # for two-argument ufuncs with a quantity and a non-quantity,
            # the quantity normally needs to be dimensionless, *except*
            # if the non-quantity can have arbitrary unit, i.e., when it
            # is all zero, infinity or NaN.  In that case, the non-quantity
            # can just have the unit of the quantity
            # (this allows, e.g., `q > 0.` independent of unit)
            maybe_arbitrary_arg = args[scales.index(0.)]
            try:
                if _can_have_arbitrary_unit(maybe_arbitrary_arg):
                    scales = [1., 1.]
                else:
                    raise UnitsError("Can only apply '{0}' function to "
                                     "dimensionless quantities when other "
                                     "argument is not a quantity (unless the "
                                     "latter is all zero/infinity/nan)"
                                     .format(function.__name__))
            except TypeError:
                # _can_have_arbitrary_unit failed: arg could not be compared
                # with zero or checked to be finite.  Then, ufunc will fail too.
                raise TypeError("Unsupported operand type(s) for ufunc {0}: "
                                "'{1}' and '{2}'"
                                .format(function.__name__,
                                        args[0].__class__.__name__,
                                        args[1].__class__.__name__))

        # In the case of np.power, the unit itself needs to be modified by an
        # amount that depends on one of the input values, so we need to treat
        # this as a special case.
        # TODO: find a better way to deal with this case
        if function is np.power and result_unit is not dimensionless_unscaled:

            if units[1] is None:
                p = args[1]
            else:
                p = args[1].to(dimensionless_unscaled).value

            result_unit = result_unit ** validate_power(p)

        # We now prepare the output object

        if self is obj:

            # this happens if the output object is self, which happens
            # for in-place operations such as q1 += q2

            # In some cases, the result of a ufunc should be a plain Numpy
            # array, which we can't do if we are doing an in-place operation.
            if result_unit is None:
                raise TypeError("Cannot store non-quantity output from {0} "
                                "function in {1} instance"
                                .format(function.__name__, type(self)))

            if self.__quantity_subclass__(result_unit)[0] is not type(self):
                raise TypeError(
                    "Cannot store output with unit '{0}' from {1} function "
                    "in {2} instance.  Use {3} instance instead."
                    .format(result_unit, function.__name__, type(self),
                            self.__quantity_subclass__(result_unit)[0]))

            # If the Quantity has an integer dtype, in-place operations are
            # dangerous because in some cases the quantity will be e.g.
            # decomposed, which involves being scaled by a float, but since
            # the array is an integer the output then gets converted to an int
            # and truncated.
            if(any(not _can_cast(arg, obj.dtype) for arg in args) or
               np.any(np.array(scales, dtype=obj.dtype) != np.array(scales))):
                raise TypeError("Arguments cannot be cast safely to inplace "
                                "output with dtype={0}".format(self.dtype))

            result = self  # no view needed since we return the object itself

            # in principle, if self is also an argument, it could be rescaled
            # here, since it won't be needed anymore.  But maybe not change
            # inputs before the calculation even if they will get destroyed

        else:  # normal case: set up output as a Quantity

            result = self._new_view(obj, result_unit)

        # We now need to treat the case where the inputs have to be scaled -
        # the issue is that we can't actually scale the inputs since that
        # would be changing the objects passed to the ufunc, which would not
        # be expected by the user.
        if any(scale != 1. for scale in scales):

            # If self is both output and input (which happens for in-place
            # operations), input will get overwritten with junk. To avoid
            # that, hide it in a new object
            if self is obj and any(self is arg for arg in args):
                # but with two outputs it would become unhidden too soon
                # [ie., np.modf(q1, q1, other)].  Bail.
                if context[2] < function.nout - 1:
                    raise TypeError("Cannot apply multi-output {0} function "
                                    "to quantities with in-place replacement "
                                    "of an input by any but the last output."
                                    .format(function.__name__))

                # If self is already contiguous, we don't need to do
                # an additional copy back into the original array, so
                # we store it in `result._result`.  Otherwise, we
                # store it in `result._contiguous`.  `__array_wrap__`
                # knows how to handle putting either form back into
                # the original array.
                if self.flags['C_CONTIGUOUS']:
                    result = self.copy()
                    result._result = self
                else:
                    result._contiguous = self.copy()

            # ensure we remember the scales we need
            result._scales = scales

        # unit output will get (setting _unit could prematurely change input
        # if obj is self, which happens for in-place operations; see above)
        result._result_unit = result_unit
        return result

    def __array_wrap__(self, obj, context=None):
        if context is not None:

            if hasattr(obj, '_result_unit'):
                result_unit = obj._result_unit
                del obj._result_unit
            else:
                result_unit = None

            # We now need to re-calculate quantities for which the input
            # needed to be scaled.
            if hasattr(obj, '_scales'):

                scales = obj._scales
                del obj._scales

                # For in-place operations, input will get overwritten with
                # junk. To avoid that, we hid it in a new object in
                # __array_prepare__ and retrieve it here.
                if hasattr(obj, '_result'):
                    obj = obj._result
                elif hasattr(obj, '_contiguous'):
                    obj[()] = obj._contiguous
                    del obj._contiguous

                # take array view to which output can be written without
                # getting back here
                obj_array = obj.view(np.ndarray)

                # Find out which ufunc was called and with which inputs
                function = context[0]
                args = context[1][:function.nin]

                # Set the inputs, rescaling as necessary
                inputs = []
                for arg, scale in zip(args, scales):
                    if scale != 1.:
                        inputs.append(arg.value * scale)
                    else:  # for scale==1, input is not necessarily a Quantity
                        inputs.append(getattr(arg, 'value', arg))

                # For output arrays that require scaling, we can reuse the
                # output array to perform the scaling in place, as long as the
                # array is not integral. Here, we set the obj_array to `None`
                # when it can not be used to store the scaled result.
                if(result_unit is not None and
                   any(not _can_cast(scaled_arg, obj_array.dtype)
                       for scaled_arg in inputs)):
                    obj_array = None

                # Re-compute the output using the ufunc
                if function.nin == 1:
                    if function.nout == 1:
                        out = function(inputs[0], obj_array)
                    else:  # 2-output function (np.modf, np.frexp); 1 input
                        if context[2] == 0:
                            out, _ = function(inputs[0], obj_array, None)
                        else:
                            _, out = function(inputs[0], None, obj_array)
                else:
                    out = function(inputs[0], inputs[1], obj_array)

                if obj_array is None:
                    obj = self._new_view(out, result_unit)

            if result_unit is None:  # return a plain array
                obj = obj.view(np.ndarray)
            else:
                obj._unit = result_unit

        return obj

    def __deepcopy__(self, memo):
        # If we don't define this, ``copy.deepcopy(quantity)`` will
        # return a bare Numpy array.
        return self.copy()

    def __quantity_subclass__(self, unit):
        """
        Overridden by subclasses to change what kind of view is
        created based on the output unit of an operation.

        Parameters
        ----------
        unit : UnitBase
            The unit for which the appropriate class should be returned

        Returns
        -------
        tuple :
            - `Quantity` subclass
            - bool: True is subclasses of the given class are ok
        """
        return Quantity, True

    def _new_view(self, obj, unit=None):
        """
        Create a Quantity view of obj, and set the unit

        By default, return a view of ``obj`` of the same class as ``self``
        and with the unit passed on, or that of ``self``.  Subclasses can
        override the type of class used with ``__quantity_subclass__``, and
        can ensure other properties of ``self`` are copied using
        `__array_finalize__`.

        Parameters
        ----------
        obj : ndarray
            The array to create a view of.  If obj is a numpy or python scalar,
            it will be converted to an array scalar.

        unit : `UnitBase`, or anything convertible to a :class:`~astropy.units.Unit`, or `None`
            The unit of the resulting object.  It is used to select a
            subclass, and explicitly assigned to the view if not `None`.
            If `None` (default), the unit is set by `__array_finalize__`
            to self._unit.

        Returns
        -------
        view : Quantity subclass
        """
        # python and numpy scalars cannot be viewed as arrays and thus not as
        # Quantity either; turn them into zero-dimensional arrays
        # (These are turned back into scalar in `.value`)
        if not isinstance(obj, np.ndarray):
            obj = np.array(obj)

        if unit is None:
            subclass = self.__class__
        else:
            unit = Unit(unit)
            subclass, subok = self.__quantity_subclass__(unit)
            if subok:
                subclass = self.__class__

        view = obj.view(subclass)
        view.__array_finalize__(self)
        if unit is not None:
            view._unit = unit
        return view

    def __reduce__(self):
        # patch to pickle Quantity objects (ndarray subclasses), see
        # http://www.mail-archive.com/numpy-discussion@scipy.org/msg02446.html

        object_state = list(super(Quantity, self).__reduce__())
        object_state[2] = (object_state[2], self.__dict__)
        return tuple(object_state)

    def __setstate__(self, state):
        # patch to unpickle Quantity objects (ndarray subclasses), see
        # http://www.mail-archive.com/numpy-discussion@scipy.org/msg02446.html

        nd_state, own_state = state
        super(Quantity, self).__setstate__(nd_state)
        self.__dict__.update(own_state)

    def to(self, unit, equivalencies=[]):
        """
        Returns a new `~astropy.units.Quantity` object with the specified
        units.

        Parameters
        ----------
        unit : `~astropy.units.UnitBase` instance, str
            An object that represents the unit to convert to. Must be
            an `~astropy.units.UnitBase` object or a string parseable
            by the `~astropy.units` package.

        equivalencies : list of equivalence pairs, optional
            A list of equivalence pairs to try if the units are not
            directly convertible.  See :ref:`unit_equivalencies`.
            If not provided or ``[]``, class default equivalencies will be used
            (none for `~astropy.units.Quantity`, but may be set for subclasses)
            If `None`, no equivalencies will be applied at all, not even any
            set globally or within a context.
        """
        if equivalencies == []:
            equivalencies = self._equivalencies
        unit = Unit(unit)
        new_val = np.asarray(
            self.unit.to(unit, self.value, equivalencies=equivalencies))
        return self._new_view(new_val, unit)

    @property
    def value(self):
        """ The numerical value of this quantity. """
        value = self.view(np.ndarray)
        if self.shape:
            return value
        else:
            return value.item()

    @property
    def unit(self):
        """
        A `~astropy.units.UnitBase` object representing the unit of this
        quantity.
        """

        return self._unit

    # this ensures that if we do a view, __repr__ and __str__ do not balk
    _unit = None

    @property
    def equivalencies(self):
        """
        A list of equivalencies that will be applied by default during
        unit conversions.
        """

        return self._equivalencies

    @property
    def si(self):
        """
        Returns a copy of the current `Quantity` instance with SI units. The
        value of the resulting object will be scaled.
        """
        si_unit = self.unit.si
        return self._new_view(self.value * si_unit.scale,
                              si_unit / si_unit.scale)

    @property
    def cgs(self):
        """
        Returns a copy of the current `Quantity` instance with CGS units. The
        value of the resulting object will be scaled.
        """
        cgs_unit = self.unit.cgs
        return self._new_view(self.value * cgs_unit.scale,
                              cgs_unit / cgs_unit.scale)

    @lazyproperty
    def isscalar(self):
        """
        True if the `value` of this quantity is a scalar, or False if it
        is an array-like object.

        .. note::
            This is subtly different from `numpy.isscalar` in that
            `numpy.isscalar` returns False for a zero-dimensional array
            (e.g. ``np.array(1)``), while this is True for quantities,
            since quantities cannot represent true numpy scalars.
        """
        return not isiterable(self.value)

    # This flag controls whether convenience conversion members, such
    # as `q.m` equivalent to `q.to(u.m).value` are available.  This is
    # not turned on on Quantity itself, but is on some subclasses of
    # Quantity, such as `astropy.coordinates.Angle`.
    _include_easy_conversion_members = False

    @override__dir__
    def __dir__(self):
        """
        Quantities are able to directly convert to other units that
        have the same physical type.  This function is implemented in
        order to make autocompletion still work correctly in IPython.
        """
        if not self._include_easy_conversion_members:
            return []
        extra_members = set()
        equivalencies = Unit._normalize_equivalencies(self.equivalencies)
        for equivalent in self.unit._get_units_with_same_physical_type(
                equivalencies):
            extra_members.update(equivalent.names)
        return extra_members

    def __getattr__(self, attr):
        """
        Quantities are able to directly convert to other units that
        have the same physical type.
        """
        if not self._include_easy_conversion_members:
            raise AttributeError(
                "'{0}' object has no '{1}' member".format(
                    self.__class__.__name__,
                    attr))

        def get_virtual_unit_attribute():
            registry = get_current_unit_registry().registry
            to_unit = registry.get(attr, None)
            if to_unit is None:
                return None

            try:
                return self.unit.to(
                    to_unit, self.value, equivalencies=self.equivalencies)
            except UnitsError:
                return None

        value = get_virtual_unit_attribute()

        if value is None:
            raise AttributeError(
                "{0} instance has no attribute '{1}'".format(
                    self.__class__.__name__, attr))
        else:
            return value

    if not NUMPY_LT_1P9:
        # Equality (return False if units do not match) needs to be handled
        # explicitly for numpy >=1.9, since it no longer traps errors.
        def __eq__(self, other):
            try:
                try:
                    return super(Quantity, self).__eq__(other)
                except DeprecationWarning:
                    # We treat the DeprecationWarning separately, since it may
                    # mask another Exception.  But we do not want to just use
                    # np.equal, since super's __eq__ treats recarrays correctly.
                    return np.equal(self, other)
            except UnitsError:
                return False
            except TypeError:
                return NotImplemented

        def __ne__(self, other):
            try:
                try:
                    return super(Quantity, self).__ne__(other)
                except DeprecationWarning:
                    return np.not_equal(self, other)
            except UnitsError:
                return True
            except TypeError:
                return NotImplemented

    # Arithmetic operations
    def __mul__(self, other):
        """ Multiplication between `Quantity` objects and other objects."""

        if isinstance(other, (UnitBase, six.string_types)):
            return self._new_view(self.copy(), other * self.unit)

        return np.multiply(self, other)

    def __imul__(self, other):
        """In-place multiplication between `Quantity` objects and others."""

        if isinstance(other, (UnitBase, six.string_types)):
            self._unit = other * self.unit
            return self

        return np.multiply(self, other, self)

    def __rmul__(self, other):
        """ Right Multiplication between `Quantity` objects and other
        objects.
        """

        return self.__mul__(other)

    def __div__(self, other):
        """ Division between `Quantity` objects and other objects."""

        if isinstance(other, (UnitBase, six.string_types)):
            return self._new_view(self.copy(), self.unit / other)

        return np.true_divide(self, other)

    def __idiv__(self, other):
        """Inplace division between `Quantity` objects and other objects."""

        if isinstance(other, (UnitBase, six.string_types)):
            self._unit = self.unit / other
            return self

        return np.true_divide(self, other, self)

    def __rdiv__(self, other):
        """ Right Division between `Quantity` objects and other objects."""

        if isinstance(other, (UnitBase, six.string_types)):
            return self._new_view(1. / self.value, other / self.unit)

        return np.divide(other, self)

    def __truediv__(self, other):
        """ Division between `Quantity` objects. """
        return self.__div__(other)

    def __itruediv__(self, other):
        """ Division between `Quantity` objects. """
        return self.__idiv__(other)

    def __rtruediv__(self, other):
        """ Division between `Quantity` objects. """
        return self.__rdiv__(other)

    def __divmod__(self, other):
        if isinstance(other, (six.string_types, UnitBase)):
            return (self / other,
                    self._new_view(np.array(0.), dimensionless_unscaled))

        other_value = self._to_own_unit(other)
        result_tuple = super(Quantity, self.__class__).__divmod__(
            self.view(np.ndarray), other_value)

        return (self._new_view(result_tuple[0], dimensionless_unscaled),
                self._new_view(result_tuple[1]))

    def __pos__(self):
        """
        Plus the quantity. This is implemented in case users use +q where q is
        a quantity.  (Required for scalar case.)
        """
        return self.copy()

    # other overrides of special functions
    def __hash__(self):
        return hash(self.value) ^ hash(self.unit)

    def __iter__(self):
        if self.isscalar:
            raise TypeError(
                "'{cls}' object with a scalar value is not iterable"
                .format(cls=self.__class__.__name__))

        # Otherwise return a generator
        def quantity_iter():
            for val in self.value:
                yield self._new_view(val)

        return quantity_iter()

    def __getitem__(self, key):
        if self.isscalar:
            raise TypeError(
                "'{cls}' object with a scalar value does not support "
                "indexing".format(cls=self.__class__.__name__))

        out = super(Quantity, self).__getitem__(key)
        return self._new_view(out)

    def __setitem__(self, i, value):
        self.view(np.ndarray).__setitem__(i, self._to_own_unit(value))

    def __setslice__(self, i, j, value):
        self.view(np.ndarray).__setslice__(i, j, self._to_own_unit(value))

    # __contains__ is OK

    def __nonzero__(self):
        """Quantities should always be treated as non-False; there is too much
        potential for ambiguity otherwise.
        """
        return True
    if six.PY3:
        __bool__ = __nonzero__

    def __len__(self):
        if self.isscalar:
            raise TypeError("'{cls}' object with a scalar value has no "
                            "len()".format(cls=self.__class__.__name__))
        else:
            return len(self.value)

    # Numerical types
    def __float__(self):
        try:
            return float(self.to(dimensionless_unscaled).value)
        except (UnitsError, TypeError):
            raise TypeError('Only dimensionless scalar quantities can be '
                            'converted to Python scalars')

    def __int__(self):
        try:
            return int(self.to(dimensionless_unscaled).value)
        except (UnitsError, TypeError):
            raise TypeError('Only dimensionless scalar quantities can be '
                            'converted to Python scalars')

    def __index__(self):
        # for indices, we do not want to mess around with scaling at all,
        # so unlike for float, int, we insist here on unscaled dimensionless
        try:
            assert self.unit.is_unity()
            return self.value.__index__()
        except:
            raise TypeError('Only integer dimensionless scalar quantities '
                            'can be converted to a Python index')

    if six.PY2:
        def __long__(self):
            try:
                return long(self.to(dimensionless_unscaled).value)
            except (UnitsError, TypeError):
                raise TypeError('Only dimensionless scalar quantities can be '
                                'converted to Python scalars')

    # Display
    # TODO: we may want to add a hook for dimensionless quantities?
    def __str__(self):
        if self.unit is None:
            unitstr = _UNIT_NOT_INITIALISED
        else:
            unitstr = self.unit.to_string()

        if unitstr:
            unitstr = ' ' + unitstr

        return '{0}{1:s}'.format(self.value, unitstr)

    def __repr__(self):
        prefixstr = '<' + self.__class__.__name__ + ' '
        arrstr = np.array2string(self.view(np.ndarray), separator=',',
                                 prefix=prefixstr)
        if self.unit is None:
            unitstr = _UNIT_NOT_INITIALISED
        else:
            unitstr = self.unit.to_string()

        if unitstr:
            unitstr = ' ' + unitstr

        return '{0}{1}{2:s}>'.format(prefixstr, arrstr, unitstr)

    def _repr_latex_(self):
        """
        Generate latex representation of the quantity and its unit.
        This is used by the IPython notebook to show it all latexified.
        It only works for scalar quantities; for arrays, the standard
        reprensation is returned.

        Returns
        -------
        lstr
            LaTeX string
        """

        if not self.isscalar:
            raise NotImplementedError('Cannot represent Quantity arrays '
                                      'in LaTex format')

        # Format value
        latex_value = "{0:g}".format(self.value)
        if "e" in latex_value:
            latex_value = latex_value.replace('e', '\\times 10^{') + '}'

        # Format unit
        # [1:-1] strips the '$' on either side needed for math mode
        latex_unit = (self.unit._repr_latex_()[1:-1]  # note this is unicode
                      if self.unit is not None
                      else _UNIT_NOT_INITIALISED)

        return '${0} \; {1}$'.format(latex_value, latex_unit)

    def __format__(self, format_spec):
        """
        Format quantities using the new-style python formatting codes
        as specifiers for the number.

        If the format specifier correctly applies itself to the value,
        then it is used to format only the value. If it cannot be
        applied to the value, then it is applied to the whole string.

        """
        try:
            value = format(self.value, format_spec)
            full_format_spec = "s"
        except ValueError:
            value = self.value
            full_format_spec = format_spec
        return format("{0} {1:s}".format(value,
                                         self.unit.to_string()
                                         if self.unit is not None
                                         else _UNIT_NOT_INITIALISED),
                      full_format_spec)

    def decompose(self, bases=[]):
        """
        Generates a new `Quantity` with the units
        decomposed. Decomposed units have only irreducible units in
        them (see `astropy.units.UnitBase.decompose`).

        Parameters
        ----------
        bases : sequence of UnitBase, optional
            The bases to decompose into.  When not provided,
            decomposes down to any irreducible units.  When provided,
            the decomposed result will only contain the given units.
            This will raises a `~astropy.units.UnitsError` if it's not possible
            to do so.

        Returns
        -------
        newq : `~astropy.units.Quantity`
            A new object equal to this quantity with units decomposed.
        """
        return self._decompose(False, bases=bases)

    def _decompose(self, allowscaledunits=False, bases=[]):
        """
        Generates a new `Quantity` with the units decomposed. Decomposed
        units have only irreducible units in them (see
        `astropy.units.UnitBase.decompose`).

        Parameters
        ----------
        allowscaledunits : bool
            If True, the resulting `Quantity` may have a scale factor
            associated with it.  If False, any scaling in the unit will
            be subsumed into the value of the resulting `Quantity`

        bases : sequence of UnitBase, optional
            The bases to decompose into.  When not provided,
            decomposes down to any irreducible units.  When provided,
            the decomposed result will only contain the given units.
            This will raises a `~astropy.units.UnitsError` if it's not possible
            to do so.

        Returns
        -------
        newq : `~astropy.units.Quantity`
            A new object equal to this quantity with units decomposed.

        """

        new_unit = self.unit.decompose(bases=bases)

        # Be careful here because self.value usually is a view of self;
        # be sure that the original value is not being modified.
        if not allowscaledunits and hasattr(new_unit, 'scale'):
            new_value = self.value * new_unit.scale
            new_unit = new_unit / new_unit.scale
            return self._new_view(new_value, new_unit)
        else:
            return self._new_view(self.copy(), new_unit)

    # These functions need to be overridden to take into account the units
    # Array conversion
    # http://docs.scipy.org/doc/numpy/reference/arrays.ndarray.html#array-conversion

    def item(self, *args):
        # item returns python built-ins, so use initializer, not _new_view
        return self.__class__(super(Quantity, self).item(*args), self.unit)

    def list(self):
        raise NotImplementedError("cannot make a list of Quantities.  Get "
                                  "list of values with q.value.list()")

    def _to_own_unit(self, value, check_precision=True):
        try:
            _value = value.to(self.unit).value
        except AttributeError:
            try:
                _value = dimensionless_unscaled.to(self.unit, value)
            except UnitsError as exc:
                if _can_have_arbitrary_unit(value):
                    _value = value
                else:
                    raise exc

        if check_precision:
            value_dtype = getattr(value, 'dtype', None)
            if self.dtype != value_dtype:
                self_dtype_array = np.array(_value, self.dtype)
                value_dtype_array = np.array(_value, dtype=value_dtype,
                                             copy=False)
                if not np.all(np.logical_or(self_dtype_array ==
                                            value_dtype_array,
                                            np.isnan(value_dtype_array))):
                    raise TypeError("cannot convert value type to array type "
                                    "without precision loss")
        return _value

    def itemset(self, *args):
        if len(args) == 0:
            raise ValueError("itemset must have at least one argument")

        self.view(np.ndarray).itemset(*(args[:-1] +
                                        (self._to_own_unit(args[-1]),)))

    def tostring(self, order='C'):
        raise NotImplementedError("cannot write Quantities to string.  Write "
                                  "array with q.value.tostring(...).")

    def tofile(self, fid, sep="", format="%s"):
        raise NotImplementedError("cannot write Quantities to file.  Write "
                                  "array with q.value.tofile(...)")

    def dump(self, file):
        raise NotImplementedError("cannot dump Quantities to file.  Write "
                                  "array with q.value.dump()")

    def dumps(self):
        raise NotImplementedError("cannot dump Quantities to string.  Write "
                                  "array with q.value.dumps()")

    # astype, byteswap, copy, view, getfield, setflags OK as is

    def fill(self, value):
        self.view(np.ndarray).fill(self._to_own_unit(value))

    # Shape manipulation: resize cannot be done (does not own data), but
    # shape, transpose, swapaxes, flatten, ravel, squeeze all OK.  Only
    # the flat iterator needs to be overwritten, otherwise single items are
    # returned as numbers.
    @property
    def flat(self):
        """A 1-D iterator over the Quantity array.

        This returns a ``QuantityIterator`` instance, which behaves the same
        as the `~numpy.flatiter` instance returned by `~numpy.ndarray.flat`,
        and is similar to, but not a subclass of, Python's built-in iterator
        object.
        """
        return QuantityIterator(self)

    @flat.setter
    def flat(self, value):
        y = self.ravel()
        y[:] = value

    # Item selection and manipulation
    # take, repeat, sort, compress, diagonal OK
    def put(self, indices, values, mode='raise'):
        self.view(np.ndarray).put(indices, self._to_own_unit(values), mode)

    def choose(self, choices, out=None, mode='raise'):
        raise NotImplementedError("cannot choose based on quantity.  Choose "
                                  "using array with q.value.choose(...)")

    # ensure we do not return indices as quantities
    def argsort(self, axis=-1, kind='quicksort', order=None):
        return self.view(np.ndarray).argsort(axis=axis, kind=kind, order=order)

    def searchsorted(self, v, *args, **kwargs):
        return np.searchsorted(np.array(self),
                               self._to_own_unit(v, check_precision=False),
                               *args, **kwargs)  # avoid numpy 1.6 problem

    # Calculation

    # ensure we do not return indices as quantities
    # conj OK

    def argmax(self, axis=None, out=None):
        return self.view(np.ndarray).argmax(axis=axis, out=out)

    def argmin(self, axis=None, out=None):
        return self.view(np.ndarray).argmin(axis=axis, out=out)

    def _prepare_out(self, out=None, unit=None):
        if out is None:
            return
        if not isinstance(out, Quantity):
            raise TypeError("out has to be assigned to a Quantity instance")
        if unit is None:
            out._unit = self._unit
        else:
            if out.__quantity_subclass__(unit)[0] is not out.__class__:
                raise TypeError("out cannot be assigned to a {0} instance; "
                                "use a {1} instance instead.".format(
                                    out.__class__,
                                    out.__quantity_subclass__(out, unit)[0]))
            out._unit = unit

    def clip(self, a_min, a_max, out=None):
        self._prepare_out(out=out)
        value = np.clip(self.value, self._to_own_unit(a_min),
                        self._to_own_unit(a_max), out=out)
        return self._new_view(value)

    def trace(self, offset=0, axis1=0, axis2=1, dtype=None, out=None):
        self._prepare_out(out=out)
        value = np.trace(self.value, offset=offset, axis1=axis1,
                         axis2=axis2, dtype=None, out=out)
        return self._new_view(value)

    def var(self, axis=None, dtype=None, out=None, ddof=0):
        result_unit = self.unit ** 2
        self._prepare_out(out=out, unit=result_unit)
        value = np.var(self.value, axis=axis, dtype=dtype, out=out, ddof=ddof),
        return self._new_view(value, result_unit)

    def std(self, axis=None, dtype=None, out=None, ddof=0):
        self._prepare_out(out=out)
        value = np.std(self.value, axis=axis, dtype=dtype, out=out, ddof=ddof)
        return self._new_view(value)

    def mean(self, axis=None, dtype=None, out=None):
        self._prepare_out(out=out)
        value = np.mean(self.value, axis=axis, dtype=dtype, out=out)
        return self._new_view(value)

    def ptp(self, axis=None, out=None):
        self._prepare_out(out=out)
        value = np.ptp(self.value, axis=axis, out=out)
        return self._new_view(value)

    def max(self, axis=None, out=None, keepdims=False):
        self._prepare_out(out=out)
        try:
            value = np.max(self.value, axis=axis, out=out, keepdims=keepdims)
        except:  # numpy < 1.7
            value = np.max(self.value, axis=axis, out=out)
        return self._new_view(value)

    def min(self, axis=None, out=None, keepdims=False):
        self._prepare_out(out=out)
        try:
            value = np.min(self.value, axis=axis, out=out, keepdims=keepdims)
        except:  # numpy < 1.7
            value = np.min(self.value, axis=axis, out=out)
        return self._new_view(value)

    def dot(self, b, out=None):
        result_unit = self.unit * getattr(b, 'unit', 1.)
        self._prepare_out(out=out, unit=result_unit)
        try:
            value = np.dot(self, b, out=out)
        except TypeError:  # numpy < 1.7
            value = np.dot(self, b)
        return self._new_view(value, result_unit)

    def diff(self, n=1, axis=-1):
        value = np.diff(self.value, n=n, axis=axis)
        return self._new_view(value)

    def ediff1d(self, to_end=None, to_begin=None):
        value = np.ediff1d(self.value, to_end=to_end, to_begin=to_begin)
        return self._new_view(value)

    def nansum(self, axis=None):
        value = np.nansum(self.value, axis=axis)
        return self._new_view(value)

    def sum(self, axis=None, dtype=None, out=None, keepdims=False):
        self._prepare_out(out=out)
        try:
            value = np.sum(self.value, axis=axis, dtype=dtype,
                           out=out, keepdims=keepdims)
        except:  # numpy < 1.7
            value = np.sum(self.value, axis=axis, dtype=dtype,
                           out=out)
        return self._new_view(value)

    def cumsum(self, axis=None, dtype=None, out=None):
        self._prepare_out(out=out)
        value = np.cumsum(self.value, axis=axis, dtype=dtype, out=out)
        return self._new_view(value)

    def prod(self, axis=None, dtype=None, out=None, keepdims=False):
        if self.unit.is_unity():
            self._prepare_out(out=out)
            try:
                value = np.prod(self.value, axis=axis, dtype=dtype,
                                out=out, keepdims=keepdims)
            except:  # numpy < 1.7
                value = np.prod(self.value, axis=axis, dtype=dtype,
                                out=out)
            return self._new_view(value)
        else:
            raise ValueError("cannot use prod on scaled or "
                             "non-dimensionless Quantity arrays")

    def cumprod(self, axis=None, dtype=None, out=None):
        if self.unit.is_unity():
            self._prepare_out(out=out)
            value = np.cumprod(self.value, axis=axis, dtype=dtype, out=out)
            return self._new_view(value)
        else:
            raise ValueError("cannot use cumprod on scaled or "
                             "non-dimensionless Quantity arrays")

    def all(self, axis=None, out=None):
        raise NotImplementedError("cannot evaluate truth value of quantities. "
                                  "Evaluate array with q.value.all(...)")

    def any(self, axis=None, out=None):
        raise NotImplementedError("cannot evaluate truth value of quantities. "
                                  "Evaluate array with q.value.any(...)")
