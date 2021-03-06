import re
from typing import Dict, List, Optional, Tuple

from collections.__init__ import OrderedDict, Counter

from .fields import RPSLTextField
from .validators import RPSLParserMessages

RPSL_ATTRIBUTE_TEXT_WIDTH = 16
TypeRPSLObjectData = List[Tuple[str, str, List[str]]]


class RPSLObjectMeta(type):
    """
    Meta class for RPSLObject (and all subclasses) for performance enhancement.

    As RPSLObject is instantiated once per object parsed, __init__ should be
    kept as small as possible. This metaclass pre-calculates some derived data
    from the fields defined by a subclass of RPSLObject, for optimised parsing speed.
    """
    def __init__(cls, name, bases, clsdict):  # noqa: N805
        super().__init__(name, bases, clsdict)
        fields = clsdict.get("fields")
        if fields:
            cls.rpsl_object_class = list(fields.keys())[0]
            cls.pk_fields = [field[0] for field in fields.items() if field[1].primary_key]
            cls.lookup_fields = [field[0] for field in fields.items() if field[1].lookup_key]
            cls.attrs_allowed = [field[0] for field in fields.items()]
            cls.attrs_required = [field[0] for field in fields.items() if not field[1].optional]
            cls.attrs_multiple = [field[0] for field in fields.items() if field[1].multiple]


class RPSLObject(metaclass=RPSLObjectMeta):
    """
    Base class for RPSL objects.

    To parse an RPSL object in string form, the best option is not to instance
    this or a subclass, but instead call rpsl_object_from_text() which
    automatically derives the correct class.

    This class should not be instanced directly - instead subclasses should be
    made for each RPSL type with the appropriate fields defined. Note that any
    subclasses should also be added to OBJECT_CLASS_MAPPING.
    """
    fields: Dict[str, RPSLTextField] = OrderedDict()
    rpsl_object_class = None
    pk_fields: List[str] = []
    attrs_allowed: List[str] = []
    attrs_required: List[str] = []
    attrs_multiple: List[str] = []

    _re_attr_name = re.compile(r"^[a-z0-9_-]+$")

    def __init__(self, from_text: Optional[str]=None, strict_validation=True) -> None:
        """
        Create a new RPSL object, optionally instantiated from a string.

        Optionally, you can set/unset strict validation. This means all
        attribute values are validated, and attribute presence/absence is
        verified. Non-strict validation is limited to primary and lookup
        keys.
        """
        self.strict_validation = strict_validation
        self.messages = RPSLParserMessages()
        self._object_data: TypeRPSLObjectData = []

        if from_text:
            self.read_rpsl_text(from_text)

    def read_rpsl_text(self, text: str) -> None:
        """Parse and validate RPSL object from string form."""
        self.messages = RPSLParserMessages()
        self._extract_attributes_values(text)
        self._validate_object()

    def pk(self) -> str:
        """Get the primary key value of an RPSL object. The PK is always converted to uppercase."""
        if len(self.pk_fields) == 1:
            return self.cleaned_data.get(self.pk_fields[0], "").upper()
        composite_values = []
        for field in self.pk_fields:
            composite_values.append(self.cleaned_data.get(field, ""))
        return ",".join(composite_values).upper()

    def render_rpsl_text(self) -> str:
        """Render the RPSL object as an RPSL string."""
        output = ""
        for attr, value, continuation_chars in self._object_data:
            attr_display = f"{attr}:".ljust(RPSL_ATTRIBUTE_TEXT_WIDTH)
            value_lines = value.splitlines()
            if not value_lines:
                output += f"{attr}:\n"
            for idx, line in enumerate(value_lines):
                if idx == 0:
                    output += attr_display + line
                else:
                    output += continuation_chars[idx-1] + (RPSL_ATTRIBUTE_TEXT_WIDTH-1) * " " + line
                output += "\n"
        return output

    def clean(self) -> bool:

        return True

    def _extract_attributes_values(self, text: str) -> None:
        """
        Extract all attributes and associated values from the input string.

        This is mostly straight forward, except for the tricky feature of line
        continuation. An attribute's value can be continued on the next lines,
        which is distinct from an attribute occurring multiple times.

        The parse result is internally stored in self._object_data. This is a
        list of 3-tuples, where each tuple contains the attribute name,
        attribute value, and the continuation characters. The continuation
        characters are needed to reconstruct the original object into a string.
        """
        continuation_chars = (" ", "+", "\t")
        current_attr = None
        current_value = ""
        current_continuation_chars: List[str] = []

        for line_no, line in enumerate(text.strip().splitlines()):
            if not line:
                self.messages.error(f"Line {line_no+1}: encountered empty line in the middle of object: [{line}]")
                return

            if not line.startswith(continuation_chars):
                if current_attr:
                    # Encountering a new attribute requires saving the previous attribute data first, if any,
                    # which can't be done earlier as line continuation means we can't know earlier whether
                    # the attribute is finished.
                    self._object_data.append((current_attr, current_value, current_continuation_chars))

                current_attr, current_value = line.split(":", maxsplit=1)
                current_attr = current_attr.lower()
                current_value = current_value.strip()
                current_continuation_chars = []

                if current_attr not in self.attrs_allowed and not self._re_attr_name.match(current_attr):
                    self.messages.error(f"Line {line_no+1}: encountered malformed attribute name: [{current_attr}]")
                    return
            else:
                # Whitespace between the continuation character and the start of the data is not significant.
                current_value += "\n" + line[1:].strip()
                current_continuation_chars += line[0]
        if current_attr:
            self._object_data.append((current_attr, current_value, current_continuation_chars))

    def _validate_object(self) -> None:
        """
        Validate an object. The strictness depends on self.strict_validation
        (see the docstring for __init__).
        """
        self.cleaned_data: Dict[str, str] = {}
        if self.strict_validation:
            self._validate_attribute_counts()
        self._clean_attribute_data()

        self.clean()

    def _validate_attribute_counts(self) -> None:
        """
        Validate the number of times each attribute occurs.

        The expected counts (0, 1, or >=1) are derived indirectly
        from the field data.
        """
        attrs_present = Counter([attr[0] for attr in self._object_data])
        for attr_name, count in attrs_present.items():
            if attr_name not in self.attrs_allowed:
                self.messages.error(f"Unrecognised attribute {attr_name} on object {self.rpsl_object_class}")
            if count > 1 and attr_name not in self.attrs_multiple:
                self.messages.error(
                    f"Attribute {attr_name} on object {self.rpsl_object_class} occurs multiple times, but is "
                    f"only allowed once")
        for attr_required in self.attrs_required:
            if attr_required not in attrs_present:
                self.messages.error(
                    f"Mandatory attribute {attr_required} on object {self.rpsl_object_class} is missing"
                )

    def _clean_attribute_data(self) -> None:
        """
        Clean the data stored in attributes.

        If self.strict_validation is not set, only checks primary and lookup keys,
        as they need to be indexed. All cleaned values (e.g. without comments) are
        stored in self.cleaned_data.
        """
        for idx, (attr_name, value, continuation_chars) in enumerate(self._object_data):
            field = self.fields.get(attr_name)
            if field and (self.strict_validation or field.primary_key or field.lookup_key):
                normalised_value = self._normalise_rpsl_value(value)
                cleaned_value = field.clean(normalised_value, self.messages)
                if cleaned_value:
                    if cleaned_value != normalised_value:
                        # Note: this cleaning can be incomplete: if the normalised value is not contained in the
                        # cleaned value as single string, the replacement will not occur. This is not a great concern,
                        # as this is purely cosmetic, and self.cleaned_data will have the correct normalised value.
                        new_value = value.replace(normalised_value, cleaned_value)
                        self._object_data[idx] = attr_name, new_value, continuation_chars
                    if attr_name in self.cleaned_data:
                        self.cleaned_data[attr_name] += "\n" + cleaned_value
                    else:
                        self.cleaned_data[attr_name] = cleaned_value

    def _normalise_rpsl_value(self, value: str) -> str:
        """
        Normalise an RPSL attribute value to its significant parts
        in a consistent format.

        For example, the following is valid in RPSL:

            inetnum: 192.0.2.0 # comment1
            +- # comment 2
            +192.0.2.1 # comment 3
            + # comment 4

        This value will be normalised by this method to:
            192.0.2.0 - 192.0.2.1
        to be used for further validation and extraction of primary keys.
        """
        normalized_lines = []
        # The shortcuts below are functionally inconsequential, but significantly improve performance,
        # as most values are single line without comments, and this method is called extremely often.
        if "\n" not in value:
            if "#" in value:
                return value.split("#")[0].strip()
            return value.strip()
        for line in value.splitlines():
            cleaned_line = line.split("#")[0].strip("\n, ")
            if cleaned_line:
                normalized_lines.append(cleaned_line)
        return ",".join(normalized_lines)

    def _update_attribute_value(self, attribute, new_values):
        """
        Update the value of an attribute in the internal state and in
        cleaned_data.

        This is used for key-cert objects, where e.g. owner lines are
        derived from other data in the object.

        All existing occurences of the attribute are removed, new items
        are always inserted at line 2 of the object.
        """
        if isinstance(new_values, str):
            new_values = [new_values]
        self.cleaned_data["attribute"] = "\n".join(new_values)

        self._object_data = list(filter(lambda a: a[0] != attribute, self._object_data))
        insert_idx = 1
        for new_value in new_values:
            self._object_data.insert(insert_idx, (attribute, new_value, []))
            insert_idx += 1
