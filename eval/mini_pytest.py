import sys, types, importlib.util, inspect
pt = types.ModuleType("pytest")
class _M:
    def parametrize(self, names, values, **k):
        def deco(f): f._params = (names, values); return f
        return deco
class _Raises:
    def __init__(self, exc): self.exc = exc
    def __enter__(self): return self
    def __exit__(self, kind, value, tb):
        if kind is None: raise AssertionError(f"DID NOT RAISE {self.exc.__name__}")
        return issubclass(kind, self.exc)
class _Monkey:
    def __init__(self): self.undo = []
    def setattr(self, target, name, value=None):
        if isinstance(target, str): raise NotImplementedError("use setattr(obj, name, value)")
        self.undo.append((target, name, getattr(target, name))); setattr(target, name, value)
    def restore(self):
        for target, name, old in reversed(self.undo): setattr(target, name, old)
pt.mark = _M(); pt.raises = _Raises; sys.modules["pytest"] = pt
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("t", path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok = bad = 0
for name, f in inspect.getmembers(m, inspect.isfunction):
    if not name.startswith("test_"): continue
    cases = [()]
    if hasattr(f, "_params"): cases = [v if isinstance(v, tuple) else (v,) for v in f._params[1]]
    for args in cases:
        mk = _Monkey() if "monkeypatch" in inspect.signature(f).parameters else None
        try: (f(mk, *args) if mk else f(*args)); ok += 1; print("PASS", name)
        except Exception as e: bad += 1; print("FAIL", name, type(e).__name__, str(e)[:300])
        finally:
            if mk: mk.restore()
print(f"{ok} passed, {bad} failed")
