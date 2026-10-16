# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

- Run the test suite: `python shieldlang.py test`
- Start ShieldLang Studio (web interface): `python shieldlang.py`
- Compile a ShieldLang source file: `python shieldlang.py compile <input.shl> --emit <target>` where target can be `py`, `js`, `report`, etc.
- On Windows, you can also double-click `run.bat`; on macOS/Linux, run `./run.sh`.

## Project Structure

- `shieldlang/` - Main compiler package containing lexer, parser, semantic analysis, IR, code generation, etc.
- `shieldlang/lang/` - Lexer, parser, AST, semantic analysis.
- `shieldlang/regex/` - RegexShield v2 sub-compiler for regex analysis and ReDoS detection.
- `shieldlang/ir/` - Intermediate representation and optimization passes.
- `shieldlang/codegen.py` - Code generation for Python and JavaScript validators.
- `shieldlang/vm.py` - Virtual machine for executing IR.
- `shieldlang/bench.py` - Benchmarking tools.
- `shieldlang/server.py` - Web server for ShieldLang Studio.
- `shieldlang/__main__.py` - Entry point.
- `web/` - Frontend assets for ShieldLang Studio (HTML, CSS, JS).
- `examples/` - Example ShieldLang programs and sample requests.
- `tests/` - Unit test suite.
- `docs/` - Documentation including grammar and architecture details.

## Important Notes

- The repository includes a comprehensive README.md with detailed usage instructions, explanations of the compiler pipeline, ReDoS detection, and more.
- For development, the code is pure Python with no external dependencies (standard library only).
- Tests can be run with `python shieldlang.py test` and include unit, differential, and fuzz tests.
