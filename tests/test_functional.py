"""pytest version of the functional tests: `pytest tests -v`.

The cases are in functional_cases.py (shared with run_functional_tests.py).
"""
import pytest

from db import get_connection
from functional_cases import CASES, Ctx


@pytest.fixture(scope="session")
def ctx():
    c = Ctx(get_connection("warehouse"), get_connection("source"))
    yield c
    c.wh.close()
    c.src.close()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case(case, ctx):
    print(case.check(ctx))
