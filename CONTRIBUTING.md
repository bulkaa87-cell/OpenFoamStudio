# Contributing to OpenFOAM Studio

Thank you for your help. This is a personal project with one maintainer, thus a reply can take some days.

## Report a bug

1. Search the open issues first. If the bug is already there, add your data to that issue.
2. Open a new issue with the "Bug report" form.
3. Give the operating system, the GPU, the OpenFOAM version, and where OpenFOAM runs (WSL, ssh server, Docker, blueCFD-Core).
4. Give the steps that cause the bug.
5. Attach the text of the "Журнал" (Log) tab. Remove host names, user names and paths that you do not want to show.

If the bug is about geometry or a mesh, attach a small STL file that shows the bug, if you can share it.

Do not report a security problem in a public issue. Obey [SECURITY.md](SECURITY.md).

## Propose a change

1. For a large change, open an issue with the "Feature request" form first. Then we can agree on the design before you write code.
2. Fork the repository and make a branch from `main`.
3. Make one change for each pull request.
4. Open a pull request and complete the checklist in the template.

## What this repository holds

This repository holds only the program files. The `.gitignore` file is an allow list: it ignores all files, except the files that it names.

- Python: `foam_studio.py`, `fs_*.py`, `requirements.txt`
- `OpenFOAM Studio.bat`
- Rust: `rust/*/src/`, `rust/*/Cargo.toml`, `rust/*/Cargo.lock`, `rust/*/.cargo/`
- `tools/build_rust.ps1`
- The community files: this file, `README.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/`

Do not add notes, settings, cases, models, test data, build output or screenshots.
If you add a new program file with a different name, add it to the allow list in `.gitignore` in the same pull request. Git ignores it silently if you do not.

## Code style

Python:

- Each module starts with `from __future__ import annotations` and uses type hints.
- A new module gets the name `fs_<topic>.py`.
- The user interface text is in Ukrainian. Keep it in Ukrainian.
- Keep logic that does not need a window out of the Qt classes, so that a test can call it.
- Do not block the Qt thread with long work. Use a worker thread, as the repair and the run code do.

Rust:

- Each module is a separate crate in `rust/<name>/`. Keep `Cargo.lock` in the repository.
- Run `cargo test` in the crate folder before you send a change. For `meshfix`, run `cargo test --no-default-features`.
- `foamview` must work on all wgpu backends (DX12, Vulkan, Metal, GL) and on software adapters. Do not use an optional wgpu feature without a check and a fallback. If `foamview` fails, the "3D" tab must use VTK.
- A wgpu error must not become a Rust panic. PyO3 changes a panic to `PanicException`, and `except Exception` does not catch it.

OpenFOAM:

- The author tests with OpenFOAM v2512 (openfoam.com). Tell us if you test with a different version.
- Do not start long runs on a computer that other people use, unless they agree.

## Commit messages

- Write commit messages in English.
- Use ASD-STE100 Simplified Technical English: short sentences, active voice, simple tenses, one word for one thing.
- The first line tells what the commit does, in the imperative form, 72 characters maximum. Example: `Fix the slice plane after a camera reset`.
- In the body, tell why you made the change and what you measured.

## Pull request checklist

- [ ] The change does one thing.
- [ ] The program starts, and the changed function works.
- [ ] `cargo test` passes for each changed crate.
- [ ] New program files are in the `.gitignore` allow list.
- [ ] No personal data: no e-mail addresses, host names, IP addresses, user names or local paths.
- [ ] Commit messages are in English (ASD-STE100).

## License

When you send a contribution, you agree that it is licensed under the GNU General Public License v3.0, as the rest of the project.
