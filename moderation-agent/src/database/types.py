from enum import Enum

from sqlalchemy import Enum as SqlEnum


def enum_type(enum: type[Enum], *, name: str) -> SqlEnum:
    return SqlEnum(
        enum,
        name=name,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
