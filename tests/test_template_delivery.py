"""Run the packaged delivery regressions from the maintained template."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'template_test_wait_reply', Path(__file__).resolve().parents[1] / 'src/skill-template/tests/test_wait_reply.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DeliveryTests = module.DeliveryTests
InputGuardTests = module.InputGuardTests
ConnectorChipTests = module.ConnectorChipTests
