"""rag-model-server serve | pull <model>... | list"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from rag_model_server.catalog import CATALOG


def pull(names: list[str], models_dir: Path) -> int:
    from huggingface_hub import hf_hub_download

    failures = 0
    for name in names:
        spec = CATALOG.get(name)
        if spec is None or not spec.sources:
            print(f"{name}: not downloadable (see the catalogue notes)")  # noqa: T201
            failures += 1
            continue
        target = models_dir / name
        if (target / "model.onnx").is_file() and (target / "tokenizer.json").is_file():
            print(f"{name}: already present in {target}")  # noqa: T201
            continue
        target.mkdir(parents=True, exist_ok=True)
        for source in spec.sources:
            try:
                onnx_path = hf_hub_download(source.repo, source.onnx)
                tok_path = hf_hub_download(source.repo, source.tokenizer)
                extras = [hf_hub_download(source.repo, extra) for extra in source.extra]
            except Exception as exc:  # noqa: BLE001 - try the next source
                print(f"{name}: {source.repo} unavailable ({type(exc).__name__}); trying next source")  # noqa: T201
                continue
            shutil.copyfile(onnx_path, target / "model.onnx")
            shutil.copyfile(tok_path, target / "tokenizer.json")
            for extra_path, extra_name in zip(extras, source.extra, strict=True):
                shutil.copyfile(extra_path, target / Path(extra_name).name)
            (target / "SOURCE").write_text(f"{source.repo}\n", encoding="utf-8")
            print(f"{name}: downloaded from {source.repo} into {target}")  # noqa: T201
            break
        else:
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rag-model-server")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("MODEL_HOST", "0.0.0.0"))  # noqa: S104 - container default
    serve.add_argument("--port", type=int, default=int(os.environ.get("MODEL_PORT", "8080")))
    pull_cmd = sub.add_parser("pull")
    pull_cmd.add_argument("models", nargs="+")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    models_dir = Path(os.environ.get("MODELS_DIR", "./models"))

    if args.command == "serve":
        import uvicorn

        from rag_model_server.app import create_app

        uvicorn.run(create_app(models_dir), host=args.host, port=args.port, log_level="info")
    elif args.command == "pull":
        sys.exit(1 if pull(args.models, models_dir) else 0)
    else:
        for name, spec in CATALOG.items():
            present = "downloaded" if (models_dir / name / "model.onnx").is_file() else "-"
            print(f"{name:28} {spec.kind:16} {present:11} {spec.notes}")  # noqa: T201


if __name__ == "__main__":
    main()
