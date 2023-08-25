import datetime
import sys
from typing import Any, List, Optional, Union

import pytest
from typing_extensions import Annotated

from airplane.config import ParamDef, _convert_task_param
from airplane.exceptions import InvalidAnnotationException
from airplane.params import (
    ParamConfig,
    ParamInfo,
    resolve_type,
    serialize_param,
    to_airplane_type,
    to_serialized_airplane_type,
)
from airplane.types import SQL


@pytest.mark.parametrize(
    "type_,resolved_type",
    [
        (
            str,
            ParamInfo(
                str,
                False,
                False,
                None,
            ),
        ),
        (
            SQL,
            ParamInfo(
                SQL,
                False,
                False,
                None,
            ),
        ),
        (
            Annotated[str, ParamConfig(slug="hello")],
            ParamInfo(
                str,
                False,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            Optional[str],
            ParamInfo(
                str,
                True,
                False,
                None,
            ),
        ),
        (
            Annotated[Optional[str], ParamConfig(slug="hello")],
            ParamInfo(
                str,
                True,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            Union[str, None],
            ParamInfo(
                str,
                True,
                False,
                None,
            ),
        ),
        (
            Annotated[Union[str, None], ParamConfig(slug="hello")],
            ParamInfo(
                str,
                True,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            Union[Optional[str], None],
            ParamInfo(
                str,
                True,
                False,
                None,
            ),
        ),
        (
            Annotated[Union[Optional[str], None], ParamConfig(slug="hello")],
            ParamInfo(
                str,
                True,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            Optional[Union[str, None]],
            ParamInfo(
                str,
                True,
                False,
                None,
            ),
        ),
        (
            Annotated[Optional[Union[str, None]], ParamConfig(slug="hello")],
            ParamInfo(
                str,
                True,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            Optional[Annotated[str, ParamConfig(slug="hello")]],
            ParamInfo(
                str,
                True,
                False,
                ParamConfig(slug="hello"),
            ),
        ),
        (
            List[str],
            ParamInfo(
                str,
                False,
                True,
                None,
            ),
        ),
        (
            Optional[List[Annotated[str, ParamConfig(slug="hello")]]],
            ParamInfo(
                str,
                True,
                True,
                ParamConfig(slug="hello"),
            ),
        ),
    ],
)
def test_resolve_type(type_: Any, resolved_type: ParamInfo) -> None:
    assert resolve_type("param", type_) == resolved_type


def test_resolve_type_errors() -> None:
    with pytest.raises(
        InvalidAnnotationException, match="Found multiple ParamConfig.*"
    ):
        resolve_type(
            "param",
            Optional[
                Annotated[
                    Annotated[str, ParamConfig(slug="hello")],
                    ParamConfig(slug="override_hello"),
                ]
            ],
        )

    with pytest.raises(InvalidAnnotationException, match="Unsupported Union.*"):
        resolve_type(
            "param",
            Union[str, int],
        )

    with pytest.raises(
        InvalidAnnotationException, match="Unsupported Optional in List"
    ):
        resolve_type(
            "param",
            List[Optional[str]],
        )

    with pytest.raises(InvalidAnnotationException, match="Unsupported List of List"):
        resolve_type(
            "param",
            List[List[str]],
        )


class CustomType:
    pass


def test_invalid_type() -> None:
    with pytest.raises(
        InvalidAnnotationException,
        match="Invalid type annotation.*",
    ):
        to_airplane_type("param", CustomType)

    with pytest.raises(
        InvalidAnnotationException,
        match="Invalid type annotation.*",
    ):
        to_serialized_airplane_type("param", CustomType)


@pytest.mark.skipif(
    sys.version_info < (3, 10), reason="requires python3.10 optional syntax"
)
def test_python_uniontype_optional() -> None:
    # Have to use eval() to avoid syntax error
    optional_type = eval("str | None")
    info = resolve_type(
        "param",
        optional_type,
    )
    assert info.is_optional
    assert info.resolved_type == str


def test_datetime_timezones() -> None:
    """
    Test that a datetime object with timezone is serialized and unserialized correctly.
    """
    dt = datetime.datetime(2022, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert dt.tzinfo is not None
    serialized_dt = serialize_param(dt)
    # The string should have the UTC timezone
    assert serialized_dt == "2022-01-01T12:00:00Z"
    # Deserialize the datetime string back into a datetime object
    deserialized_dt = _convert_task_param(
        ParamDef(
            arg_name="required",
            slug="required",
            name="Required",
            type="datetime",
            description="",
            default=None,
            multi=False,
            required=True,
            options=None,
            regex=None,
        ),
        serialized_dt,
    )
    assert deserialized_dt.tzinfo is not None
    assert deserialized_dt == dt
