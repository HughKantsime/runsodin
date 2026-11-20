"""Supervised Python processes must map relationships before starting workers."""
import ast
from configparser import ConfigParser
from pathlib import Path
import shlex

import pytest

ROOT = Path(__file__).resolve().parents[2]


def supervised_modules():
    modules = set()
    for path in (ROOT / "docker").glob("supervisord*.conf"):
        config = ConfigParser(interpolation=None)
        config.read(path)
        for section in config.sections():
            cmd = shlex.split(config.get(section, "command", fallback=""))
            if cmd[:2] == ["python3", "-m"]:
                if cmd[2] == "uvicorn":
                    assert cmd[3] == "main:app"
                modules.add(cmd[2])
    return sorted(modules)


@pytest.mark.parametrize("module", supervised_modules())
def test_configure_orm_before_supervised_loop(module):
    def parse(relative):
        return ast.parse((ROOT / "backend" / relative).read_text())

    if module == "uvicorn":
        entry = ast.unparse(parse("main.py"))
        assert "from core.app import create_app" in entry and "app = create_app()" in entry
        app = parse("core/app.py")
        assert any(isinstance(n, ast.Call) and ast.unparse(n.func) == "FastAPI"
                   and any(k.arg == "lifespan" and ast.unparse(k.value) == "lifespan" for k in n.keywords)
                   for n in ast.walk(app))
        block = next(n for n in ast.walk(app) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    else:
        tree = parse(module.replace(".", "/") + ".py")
        block = next(n for n in tree.body if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "__name__ == '__main__'")
    imported = False
    for statement in block.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "core.schema.bootstrap":
            imported = any(n.name == "configure_orm" and not n.asname for n in statement.names)
        if ast.unparse(statement) == "configure_orm()":
            assert imported
            break
        for node in ast.walk(statement):
            assert not isinstance(node, (ast.While, ast.Yield)), f"{module} starts its loop before configuring the ORM"
            if isinstance(node, ast.Call):
                launch = ast.unparse(node.func).split(".")[-1]
                assert launch not in {"main", "main_loop", "start", "run", "run_forever", "create_task"}, module
    else:
        pytest.fail(f"{module} starts without configuring the ORM")
