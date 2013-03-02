from __future__ import unicode_literals
import copy
import datetime
import types
from decimal import Decimal
from django.core.paginator import Page
from django.db import models
from django.forms import widgets
from django.utils.datastructures import SortedDict
from rest_framework.compat import get_concrete_model
from rest_framework.compat import six

# Note: We do the following so that users of the framework can use this style:
#
#     example_field = serializers.CharField(...)
#
# This helps keep the seperation between model fields, form fields, and
# serializer fields more explicit.

from rest_framework.relations import *
from rest_framework.fields import *


class DictWithMetadata(dict):
    """
    A dict-like object, that can have additional properties attached.
    """
    def __getstate__(self):
        """
        Used by pickle (e.g., caching).
        Overriden to remove the metadata from the dict, since it shouldn't be
        pickled and may in some instances be unpickleable.
        """
        return dict(self)


class SortedDictWithMetadata(SortedDict):
    """
    A sorted dict-like object, that can have additional properties attached.
    """
    def __getstate__(self):
        """
        Used by pickle (e.g., caching).
        Overriden to remove the metadata from the dict, since it shouldn't be
        pickle and may in some instances be unpickleable.
        """
        return SortedDict(self).__dict__


def _is_protected_type(obj):
    """
    True if the object is a native datatype that does not need to
    be serialized further.
    """
    return isinstance(obj, (
        types.NoneType,
        int, long,
        datetime.datetime, datetime.date, datetime.time,
        float, Decimal,
        basestring)
    )


def _get_declared_fields(bases, attrs):
    """
    Create a list of serializer field instances from the passed in 'attrs',
    plus any fields on the base classes (in 'bases').

    Note that all fields from the base classes are used.
    """
    fields = [(field_name, attrs.pop(field_name))
              for field_name, obj in list(six.iteritems(attrs))
              if isinstance(obj, Field)]
    fields.sort(key=lambda x: x[1].creation_counter)

    # If this class is subclassing another Serializer, add that Serializer's
    # fields.  Note that we loop over the bases in *reverse*. This is necessary
    # in order to maintain the correct order of fields.
    for base in bases[::-1]:
        if hasattr(base, 'base_fields'):
            fields = list(base.base_fields.items()) + fields

    return SortedDict(fields)


class SerializerMetaclass(type):
    def __new__(cls, name, bases, attrs):
        attrs['base_fields'] = _get_declared_fields(bases, attrs)
        return super(SerializerMetaclass, cls).__new__(cls, name, bases, attrs)


class SerializerOptions(object):
    """
    Meta class options for Serializer
    """
    def __init__(self, meta):
        self.depth = getattr(meta, 'depth', 0)
        self.fields = getattr(meta, 'fields', ())
        self.exclude = getattr(meta, 'exclude', ())


class BaseSerializer(WritableField):
    """
    This is the Serializer implementation.
    We need to implement it as `BaseSerializer` due to metaclass magicks.
    """
    class Meta(object):
        pass

    _options_class = SerializerOptions
    _dict_class = SortedDictWithMetadata

    def __init__(self, instance=None, data=None, files=None,
                 context=None, partial=False, many=None, source=None):
        super(BaseSerializer, self).__init__(source=source)
        self.opts = self._options_class(self.Meta)
        self.parent = None
        self.root = None
        self.partial = partial
        self.many = many

        self.context = context or {}

        self.init_data = data
        self.init_files = files
        self.object = instance
        self.fields = self.get_fields()

        self._data = None
        self._files = None
        self._errors = None
        self._delete = False

    #####
    # Methods to determine which fields to use when (de)serializing objects.

    def get_default_fields(self):
        """
        Return the complete set of default fields for the object, as a dict.
        """
        return {}

    def get_fields(self):
        """
        Returns the complete set of fields for the object as a dict.

        This will be the set of any explicitly declared fields,
        plus the set of fields returned by get_default_fields().
        """
        ret = SortedDict()

        # Get the explicitly declared fields
        base_fields = copy.deepcopy(self.base_fields)
        for key, field in base_fields.items():
            ret[key] = field

        # Add in the default fields
        default_fields = self.get_default_fields()
        for key, val in default_fields.items():
            if key not in ret:
                ret[key] = val

        # If 'fields' is specified, use those fields, in that order.
        if self.opts.fields:
            assert isinstance(self.opts.fields, (list, tuple)), '`include` must be a list or tuple'
            new = SortedDict()
            for key in self.opts.fields:
                new[key] = ret[key]
            ret = new

        # Remove anything in 'exclude'
        if self.opts.exclude:
            assert isinstance(self.opts.fields, (list, tuple)), '`exclude` must be a list or tuple'
            for key in self.opts.exclude:
                ret.pop(key, None)

        for key, field in ret.items():
            field.initialize(parent=self, field_name=key)

        return ret

    #####
    # Field methods - used when the serializer class is itself used as a field.

    def initialize(self, parent, field_name):
        """
        Same behaviour as usual Field, except that we need to keep track
        of state so that we can deal with handling maximum depth.
        """
        super(BaseSerializer, self).initialize(parent, field_name)
        if parent.opts.depth:
            self.opts.depth = parent.opts.depth - 1

    #####
    # Methods to convert or revert from objects <--> primitive representations.

    def get_field_key(self, field_name):
        """
        Return the key that should be used for a given field.
        """
        return field_name

    def restore_fields(self, data, files):
        """
        Core of deserialization, together with `restore_object`.
        Converts a dictionary of data into a dictionary of deserialized fields.
        """
        reverted_data = {}

        if data is not None and not isinstance(data, dict):
            self._errors['non_field_errors'] = ['Invalid data']
            return None

        for field_name, field in self.fields.items():
            field.initialize(parent=self, field_name=field_name)
            try:
                field.field_from_native(data, files, field_name, reverted_data)
            except ValidationError as err:
                if hasattr(err, 'message_dict'):
                    self._errors[field_name] = [err.message_dict]
                else:
                    self._errors[field_name] = list(err.messages)

        return reverted_data

    def perform_validation(self, attrs):
        """
        Run `validate_<fieldname>()` and `validate()` methods on the serializer
        """
        for field_name, field in self.fields.items():
            if field_name in self._errors:
                continue
            try:
                validate_method = getattr(self, 'validate_%s' % field_name, None)
                if validate_method:
                    source = field.source or field_name
                    attrs = validate_method(attrs, source)
            except ValidationError as err:
                self._errors[field_name] = self._errors.get(field_name, []) + list(err.messages)

        # If there are already errors, we don't run .validate() because
        # field-validation failed and thus `attrs` may not be complete.
        # which in turn can cause inconsistent validation errors.
        if not self._errors:
            try:
                attrs = self.validate(attrs)
            except ValidationError as err:
                if hasattr(err, 'message_dict'):
                    for field_name, error_messages in err.message_dict.items():
                        self._errors[field_name] = self._errors.get(field_name, []) + list(error_messages)
                elif hasattr(err, 'messages'):
                    self._errors['non_field_errors'] = err.messages

        return attrs

    def validate(self, attrs):
        """
        Stub method, to be overridden in Serializer subclasses
        """
        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Deserialize a dictionary of attributes into an object instance.
        You should override this method to control how deserialized objects
        are instantiated.
        """
        if instance is not None:
            instance.update(attrs)
            return instance
        return attrs

    def to_native(self, obj):
        """
        Serialize objects -> primitives.
        """
        ret = self._dict_class()
        ret.fields = {}

        for field_name, field in self.fields.items():
            field.initialize(parent=self, field_name=field_name)
            key = self.get_field_key(field_name)
            value = field.field_to_native(obj, field_name)
            ret[key] = value
            ret.fields[key] = field
        return ret

    def from_native(self, data, files):
        """
        Deserialize primitives -> objects.
        """
        if hasattr(data, '__iter__') and not isinstance(data, (dict, six.text_type)):
            # TODO: error data when deserializing lists
            return [self.from_native(item, None) for item in data]

        self._errors = {}
        if data is not None or files is not None:
            attrs = self.restore_fields(data, files)
            attrs = self.perform_validation(attrs)
        else:
            self._errors['non_field_errors'] = ['No input provided']

        if not self._errors:
            return self.restore_object(attrs, instance=getattr(self, 'object', None))

    def field_to_native(self, obj, field_name):
        """
        Override default so that we can apply ModelSerializer as a nested
        field to relationships.
        """
        if self.source == '*':
            return self.to_native(obj)

        try:
            if self.source:
                for component in self.source.split('.'):
                    obj = getattr(obj, component)
                    if is_simple_callable(obj):
                        obj = obj()
            else:
                obj = getattr(obj, field_name)
                if is_simple_callable(obj):
                    obj = obj()
        except ObjectDoesNotExist:
            return None

        # If the object has an "all" method, assume it's a relationship
        if is_simple_callable(getattr(obj, 'all', None)):
            return [self.to_native(item) for item in obj.all()]

        if obj is None:
            return None

        if self.many is not None:
            many = self.many
        else:
            many = hasattr(obj, '__iter__') and not isinstance(obj, (Page, dict))

        if many:
            return [self.to_native(item) for item in obj]
        return self.to_native(obj)

    @property
    def errors(self):
        """
        Run deserialization and return error data,
        setting self.object if no errors occurred.
        """
        if self._errors is None:
            data, files = self.init_data, self.init_files

            if self.many is not None:
                many = self.many
            else:
                many = hasattr(data, '__iter__') and not isinstance(data, (Page, dict))
                if many:
                    warnings.warn('Implict list/queryset serialization is due to be deprecated. '
                                  'Use the `many=True` flag when instantiating the serializer.',
                                  PendingDeprecationWarning, stacklevel=3)

            # TODO: error data when deserializing lists
            if many:
                ret = [self.from_native(item, None) for item in data]
            ret = self.from_native(data, files)

            if not self._errors:
                self.object = ret
        return self._errors

    def is_valid(self):
        return not self.errors

    @property
    def data(self):
        """
        Returns the serialized data on the serializer.
        """
        if self._data is None:
            obj = self.object

            if self.many is not None:
                many = self.many
            else:
                many = hasattr(obj, '__iter__') and not isinstance(obj, (Page, dict))
                if many:
                    warnings.warn('Implict list/queryset serialization is due to be deprecated. '
                                  'Use the `many=True` flag when instantiating the serializer.',
                                  PendingDeprecationWarning, stacklevel=2)

            if many:
                self._data = [self.to_native(item) for item in obj]
            else:
                self._data = self.to_native(obj)

        return self._data

    def save(self):
        """
        Save the deserialized object and return it.
        """
        self.object.save()
        return self.object


class Serializer(six.with_metaclass(SerializerMetaclass, BaseSerializer)):
    pass


class ModelSerializerOptions(SerializerOptions):
    """
    Meta class options for ModelSerializer
    """
    def __init__(self, meta):
        super(ModelSerializerOptions, self).__init__(meta)
        self.model = getattr(meta, 'model', None)
        self.read_only_fields = getattr(meta, 'read_only_fields', ())


class ModelSerializer(Serializer):
    """
    A serializer that deals with model instances and querysets.
    """
    _options_class = ModelSerializerOptions

    field_mapping = {
        models.AutoField: IntegerField,
        models.FloatField: FloatField,
        models.IntegerField: IntegerField,
        models.PositiveIntegerField: IntegerField,
        models.SmallIntegerField: IntegerField,
        models.PositiveSmallIntegerField: IntegerField,
        models.DateTimeField: DateTimeField,
        models.DateField: DateField,
        models.TimeField: TimeField,
        models.EmailField: EmailField,
        models.CharField: CharField,
        models.URLField: URLField,
        models.SlugField: SlugField,
        models.TextField: CharField,
        models.CommaSeparatedIntegerField: CharField,
        models.BooleanField: BooleanField,
        models.FileField: FileField,
        models.ImageField: ImageField,
    }

    def field_from_native(self, data, files, field_name, into):
        if self.read_only:
            return

        try:
            value = data[field_name]
        except KeyError:
            if self.required:
                raise ValidationError(self.error_messages['required'])
            return

        if self.parent.object:
            # Set the serializer object if it exists
            obj = getattr(self.parent.object, field_name)
            self.object = obj

        if value in (None, ''):
            self._delete = True
            into[(self.source or field_name)] = self
        else:
            obj = self.from_native(value, files)
            if not self._errors:
                self.object = obj
                into[self.source or field_name] = self
            else:
                # Propagate errors up to our parent
                raise ValidationError(self._errors)

    def get_default_fields(self):
        """
        Return all the fields that should be serialized for the model.
        """

        cls = self.opts.model
        assert cls is not None, \
                "Serializer class '%s' is missing 'model' Meta option" % self.__class__.__name__
        opts = get_concrete_model(cls)._meta
        pk_field = opts.pk
        # while pk_field.rel:
        #     pk_field = pk_field.rel.to._meta.pk
        fields = [pk_field]
        fields += [field for field in opts.fields if field.serialize]
        fields += [field for field in opts.many_to_many if field.serialize]

        ret = SortedDict()
        nested = bool(self.opts.depth)
        is_pk = True  # First field in the list is the pk

        for model_field in fields:
            if is_pk:
                field = self.get_pk_field(model_field)
                is_pk = False
            elif model_field.rel and nested:
                field = self.get_nested_field(model_field)
            elif model_field.rel:
                to_many = isinstance(model_field,
                                     models.fields.related.ManyToManyField)
                field = self.get_related_field(model_field, to_many=to_many)
            else:
                field = self.get_field(model_field)

            if field:
                ret[model_field.name] = field

        for field_name in self.opts.read_only_fields:
            assert field_name in ret, \
                "read_only_fields on '%s' included invalid item '%s'" % \
                (self.__class__.__name__, field_name)
            ret[field_name].read_only = True

        return ret

    def get_pk_field(self, model_field):
        """
        Returns a default instance of the pk field.
        """
        return self.get_field(model_field)

    def get_nested_field(self, model_field):
        """
        Creates a default instance of a nested relational field.
        """
        class NestedModelSerializer(ModelSerializer):
            class Meta:
                model = model_field.rel.to
        return NestedModelSerializer()

    def get_related_field(self, model_field, to_many=False):
        """
        Creates a default instance of a flat relational field.
        """
        # TODO: filter queryset using:
        # .using(db).complex_filter(self.rel.limit_choices_to)
        kwargs = {
            'required': not(model_field.null or model_field.blank),
            'queryset': model_field.rel.to._default_manager,
            'many': to_many
        }

        return PrimaryKeyRelatedField(**kwargs)

    def get_field(self, model_field):
        """
        Creates a default instance of a basic non-relational field.
        """
        kwargs = {}
        has_default = model_field.has_default()

        if model_field.null or model_field.blank or has_default:
            kwargs['required'] = False

        if isinstance(model_field, models.AutoField) or not model_field.editable:
            kwargs['read_only'] = True

        if has_default:
            kwargs['default'] = model_field.get_default()

        if issubclass(model_field.__class__, models.TextField):
            kwargs['widget'] = widgets.Textarea

        # TODO: TypedChoiceField?
        if model_field.flatchoices:  # This ModelField contains choices
            kwargs['choices'] = model_field.flatchoices
            return ChoiceField(**kwargs)

        try:
            return self.field_mapping[model_field.__class__](**kwargs)
        except KeyError:
            return ModelField(model_field=model_field, **kwargs)

    def get_validation_exclusions(self):
        """
        Return a list of field names to exclude from model validation.
        """
        cls = self.opts.model
        opts = get_concrete_model(cls)._meta
        exclusions = [field.name for field in opts.fields + opts.many_to_many]
        for field_name, field in self.fields.items():
            field_name = field.source or field_name
            if field_name in exclusions and not field.read_only:
                exclusions.remove(field_name)
        return exclusions

    def full_clean(self, instance):
        """
        Perform Django's full_clean, and populate the `errors` dictionary
        if any validation errors occur.

        Note that we don't perform this inside the `.restore_object()` method,
        so that subclasses can override `.restore_object()`, and still get
        the full_clean validation checking.
        """
        try:
            instance.full_clean(exclude=self.get_validation_exclusions())
        except ValidationError as err:
            self._errors = err.message_dict
            return None
        return instance

    def restore_object(self, attrs, instance=None):
        """
        Restore the model instance.
        """
        self.m2m_data = {}
        self.related_data = {}

        # Reverse fk relations
        for (obj, model) in self.opts.model._meta.get_all_related_objects_with_model():
            field_name = obj.field.related_query_name()
            if field_name in attrs:
                self.related_data[field_name] = attrs.pop(field_name)

        # Reverse m2m relations
        for (obj, model) in self.opts.model._meta.get_all_related_m2m_objects_with_model():
            field_name = obj.field.related_query_name()
            if field_name in attrs:
                self.m2m_data[field_name] = attrs.pop(field_name)

        # Forward m2m relations
        for field in self.opts.model._meta.many_to_many:
            if field.name in attrs:
                self.m2m_data[field.name] = attrs.pop(field.name)

        if instance is not None:
            for key, val in attrs.items():
                setattr(instance, key, val)

        else:
            instance = self.opts.model(**attrs)

        return instance

    def from_native(self, data, files):
        """
        Override the default method to also include model field validation.
        """
        instance = super(ModelSerializer, self).from_native(data, files)
        if instance:
            return self.full_clean(instance)

    def _save(self, parent=None, fk_field=None):
        if self._delete:
            self.object.delete()
            return

        if parent and fk_field:
            setattr(self.object, fk_field, parent)

        self.object.save()

        if getattr(self, 'm2m_data', None):
            for accessor_name, object_list in self.m2m_data.items():
                setattr(self.object, accessor_name, object_list)
            self.m2m_data = {}

        if getattr(self, 'related_data', None):
            for accessor_name, object_list in self.related_data.items():
                if isinstance(object_list, ModelSerializer):
                    fk_field = self.object._meta.get_field_by_name(accessor_name)[0].field.name
                    object_list._save(parent=self.object, fk_field=fk_field)
                else:
                    setattr(self.object, accessor_name, object_list)
            self.related_data = {}
            
    def save(self):
        """
        Save the deserialized object and return it.
        """
        self._save()
        return self.object


class HyperlinkedModelSerializerOptions(ModelSerializerOptions):
    """
    Options for HyperlinkedModelSerializer
    """
    def __init__(self, meta):
        super(HyperlinkedModelSerializerOptions, self).__init__(meta)
        self.view_name = getattr(meta, 'view_name', None)


class HyperlinkedModelSerializer(ModelSerializer):
    """
    A subclass of ModelSerializer that uses hyperlinked relationships,
    instead of primary key relationships.
    """
    _options_class = HyperlinkedModelSerializerOptions
    _default_view_name = '%(model_name)s-detail'

    url = HyperlinkedIdentityField()

    def __init__(self, *args, **kwargs):
        super(HyperlinkedModelSerializer, self).__init__(*args, **kwargs)
        if self.opts.view_name is None:
            self.opts.view_name = self._get_default_view_name(self.opts.model)

    def _get_default_view_name(self, model):
        """
        Return the view name to use if 'view_name' is not specified in 'Meta'
        """
        model_meta = model._meta
        format_kwargs = {
            'app_label': model_meta.app_label,
            'model_name': model_meta.object_name.lower()
        }
        return self._default_view_name % format_kwargs

    def get_pk_field(self, model_field):
        return None

    def get_related_field(self, model_field, to_many):
        """
        Creates a default instance of a flat relational field.
        """
        # TODO: filter queryset using:
        # .using(db).complex_filter(self.rel.limit_choices_to)
        rel = model_field.rel.to
        kwargs = {
            'required': not(model_field.null or model_field.blank),
            'queryset': rel._default_manager,
            'view_name': self._get_default_view_name(rel),
            'many': to_many
        }
        return HyperlinkedRelatedField(**kwargs)
