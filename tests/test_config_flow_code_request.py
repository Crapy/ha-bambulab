"""Regression tests for choosing an emailed/SMS code during cloud login.

The existing unit suite intentionally does not depend on Home Assistant. Load
the unchanged async_step_Bambu method bodies from config_flow.py, then execute
them with doubles only at the HA form/executor and cloud API boundaries. This
tests the production routing and form fields without importing the entire HA
integration, making network calls, or copying the routing implementation here.
Full HA schema validation and integration startup are outside this test's scope.
"""

from __future__ import annotations

import ast
from collections import OrderedDict
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "bambu_lab"
    / "config_flow.py"
)


def _load_step(handler_name: str):
    """Compile the real handler body while leaving HA imports out of this suite."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == handler_name
    )
    step = next(
        node
        for node in handler.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_Bambu"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            step,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    # The tests inspect field names, not voluptuous or HA selector internals.
    def field_name(name, **_kwargs):
        return name

    namespace = {
        "OrderedDict": OrderedDict,
        "LOGGER": Mock(),
        "vol": SimpleNamespace(Optional=field_name, Required=field_name, Schema=dict),
        "BOOLEAN_SELECTOR": object(),
        "TEXT_SELECTOR": object(),
        "REGION_SELECTOR": object(),
        "PASSWORD_SELECTOR": object(),
    }
    for name in (
        "CloudflareError",
        "CurlUnavailableError",
        "CodeRequiredError",
        "CodeExpiredError",
        "CodeIncorrectError",
        "CsrfError",
        "TfaCodeRequiredError",
    ):
        namespace[name] = type(name, (Exception,), {})

    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["async_step_Bambu"]


class _CodeRequestCases:
    """The same authentication contract applies to config and options flows."""

    handler_name: str
    next_step_name: str

    def make_flow(self, authentication_type="tfaCode"):
        cloud = SimpleNamespace(
            request_new_code=Mock(),
            login_with_verification_code=Mock(),
            login_with_2fa_code=Mock(),
            login=Mock(),
        )

        async def run_executor_job(function, *args):
            return function(*args)

        flow = SimpleNamespace(
            authentication_type=authentication_type,
            region="Europe",
            email="printer@example.invalid",
            _config_entry=SimpleNamespace(options={}),
            _bambu_cloud=cloud,
            hass=SimpleNamespace(async_add_executor_job=run_executor_job),
            async_show_form=lambda **kwargs: {"type": "form", **kwargs},
            async_abort=lambda **kwargs: {"type": "abort", **kwargs},
            async_step_Bambu_Choose_Device=AsyncMock(return_value={"step": "choose"}),
            async_step_Bambu_Lan=AsyncMock(return_value={"step": "lan"}),
        )
        flow.async_step_Bambu = MethodType(_load_step(self.handler_name), flow)
        return flow

    async def test_requested_email_code_changes_form_and_submission_route(self):
        flow = self.make_flow()

        # A request must not submit a stale code still present in the old form.
        result = await flow.async_step_Bambu({"newCode": True, "tfaCode": "999999"})

        self.assertEqual(flow.authentication_type, "verifyCode")
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "verifyCode"})
        self.assertEqual(set(result["data_schema"]), {"newCode", "verifyCode"})
        flow._bambu_cloud.request_new_code.assert_called_once_with()
        flow._bambu_cloud.login_with_verification_code.assert_not_called()
        flow._bambu_cloud.login_with_2fa_code.assert_not_called()

        result = await flow.async_step_Bambu({"newCode": False, "verifyCode": "001234"})

        flow._bambu_cloud.login_with_verification_code.assert_called_once_with("001234")
        flow._bambu_cloud.login_with_2fa_code.assert_not_called()
        flow._bambu_cloud.login.assert_not_called()
        expected_step = getattr(flow, self.next_step_name)
        expected_step.assert_awaited_once_with(None)
        self.assertEqual(result, expected_step.return_value)

    async def test_failed_request_preserves_the_existing_code_form(self):
        for authentication_type in ("tfaCode", "verifyCode"):
            with self.subTest(authentication_type=authentication_type):
                flow = self.make_flow(authentication_type)
                flow._bambu_cloud.request_new_code.side_effect = PermissionError(
                    429, "Code request unavailable"
                )

                result = await flow.async_step_Bambu({"newCode": True})

                self.assertEqual(flow.authentication_type, authentication_type)
                self.assertEqual(result["errors"], {"base": "cannot_connect"})
                self.assertEqual(
                    set(result["data_schema"]), {"newCode", authentication_type}
                )
                flow._bambu_cloud.request_new_code.assert_called_once_with()
                flow._bambu_cloud.login_with_verification_code.assert_not_called()
                flow._bambu_cloud.login_with_2fa_code.assert_not_called()
                flow.async_step_Bambu_Choose_Device.assert_not_awaited()
                flow.async_step_Bambu_Lan.assert_not_awaited()

    async def test_resending_email_code_keeps_the_verification_form(self):
        flow = self.make_flow("verifyCode")

        result = await flow.async_step_Bambu({"newCode": True})

        self.assertEqual(flow.authentication_type, "verifyCode")
        self.assertEqual(result["errors"], {"base": "verifyCode"})
        self.assertEqual(set(result["data_schema"]), {"newCode", "verifyCode"})
        flow._bambu_cloud.request_new_code.assert_called_once_with()
        flow._bambu_cloud.login_with_verification_code.assert_not_called()
        flow._bambu_cloud.login_with_2fa_code.assert_not_called()

    async def test_normal_two_factor_submission_keeps_the_two_factor_route(self):
        flow = self.make_flow()

        result = await flow.async_step_Bambu({"newCode": False, "tfaCode": "003456"})

        self.assertEqual(flow.authentication_type, "tfaCode")
        flow._bambu_cloud.login_with_2fa_code.assert_called_once_with("003456")
        flow._bambu_cloud.login_with_verification_code.assert_not_called()
        flow._bambu_cloud.request_new_code.assert_not_called()
        flow._bambu_cloud.login.assert_not_called()
        expected_step = getattr(flow, self.next_step_name)
        expected_step.assert_awaited_once_with(None)
        self.assertEqual(result, expected_step.return_value)


class TestConfigCodeRequest(_CodeRequestCases, unittest.IsolatedAsyncioTestCase):
    handler_name = "BambuLabFlowHandler"
    next_step_name = "async_step_Bambu_Choose_Device"


class TestOptionsCodeRequest(_CodeRequestCases, unittest.IsolatedAsyncioTestCase):
    handler_name = "BambuOptionsFlowHandler"
    next_step_name = "async_step_Bambu_Lan"


if __name__ == "__main__":
    unittest.main()
