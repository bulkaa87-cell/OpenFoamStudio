# OpenFOAM Studio

OpenFOAM Studio is a desktop program for external aerodynamics with OpenFOAM.
You load an STL model, examine and repair it, set the domain, the mesh and the flow.
Then the program writes the OpenFOAM case, runs it, and shows the results.

The user interface of the program is in English and Ukrainian.

## Status

- This is a personal project. One person writes it and uses it.
- The main platform is Windows 10/11. Other platforms are not tested.
- The author tests the program with OpenFOAM v2512 (openfoam.com).
- Some parts are experimental. The list below marks them.

## Features

- STL import with a units question (mm, m and others), a model list and transforms
- Geometry checks: holes, inverted faces, non-manifold edges, degenerate faces, with highlights on the model
- Geometry repair:
  - Built-in repair (VTK)
  - "Repair as a slicer": the mesh repair service of Windows (WinRT `Printing3D`), optional
  - meshfix: the own repair of the program in Rust. The result is close to the Windows 3D-model repair; this is checked by measurements on test meshes (**experimental**, see "Known problems")
  - Alpha wrap through the external `wrapwrap` program (CGAL), optional
- Domain and background mesh, `snappyHexMesh` with refinement levels and prism layers
- Solvers: `simpleFoam`, `pimpleFoam`, `rhoSimpleFoam`, `rhoPimpleFoam`, `rhoCentralFoam` (supersonic)
- Turbulence models: `kOmegaSST`, `kOmegaSSTLM`, `kEpsilon`, `SpalartAllmaras`, DDES/IDDES, LES (`WALE`, `kEqn`)
- An advisor that calculates the mesh and solver settings from the geometry and the flow
- A beginner mode (F2) with an explanation for each setting
- Run control: pause, resume, stop, and a stop that writes the last time step
- Results: forces and coefficients, residual plots, slices, streamlines, animation of transient results
- A library of saved simulations
- Rust modules for speed (all optional):
  - `foamrs`: fast reading of OpenFOAM fields
  - `foamview`: the "3D" tab drawn on the GPU with wgpu. If it cannot start, the tab uses VTK.
  - `meshfix`: mesh repair (**experimental**)
  - `foamdist`: one solver run on several Linux machines (**experimental**)

## Where OpenFOAM runs

The program does not include OpenFOAM. It finds an installation and runs it.
You select the installation on the "Запуск" (Run) tab.

- **WSL** on Windows. If OpenFOAM is not installed, the tab shows an install button.
  The button opens a WSL console and installs the latest release package from openfoam.com.
  The console asks for your sudo password.
- **A Linux server over ssh.** The program uses the `ssh` command of your system with key login (`BatchMode=yes`).
  OpenFOAM must be installed on the server.
  The cases go to `~/openfoam-studio/` on the server.
- **Docker** on the local computer, with an OpenFOAM image.
- **blueCFD-Core** on Windows.

### Distributed run (experimental)

The group "Розподілити між машинами (експериментально)" on the "Запуск" tab divides one solver run between several Linux machines.
It works only with the ssh server mode.
The `foamdist` program runs MPI in containers (image `docker.io/opencfd/openfoam-run:2512`, with Docker or Podman) on each machine.
The server must have an ssh key that opens all other machines.
Pause and resume do not work for a distributed run.

## Requirements

- Windows 10 or 11
- Python 3.12 (tested). The dependencies and the Rust modules need Python 3.9 or later, but the author does not test old versions.
- OpenFOAM v2512 in WSL, or on a Linux server that you can reach over ssh (or Docker, or blueCFD-Core)
- The Python packages in `requirements.txt`. The two `winrt-*` packages are optional. Without them, the button "Полагодити як слайсер" is disabled.
- A Rust toolchain, only if you want to build the Rust modules
- `wrapwrap` (https://github.com/ipadjen/wrapwrap), only for the alpha wrap repair. Put `wrapwrap.exe` in `tools\` or in `PATH`.

## Download (Windows)

The Windows x64 installer is on the [Releases](https://github.com/bulkaa87-cell/OpenFoamStudio/releases) page.
The installer has no signature. If Windows SmartScreen stops it, click **More info**, then **Run anyway**.
The install guide and the server setup are in [docs/INSTALL.en.md](docs/INSTALL.en.md) (Ukrainian: [docs/INSTALL.uk.md](docs/INSTALL.uk.md)).
macOS and Linux installers come later. On these systems, run the program from the source (see below).

## Install and run from the source

1. Install Python and Git.
2. Clone the repository:

   ```
   git clone https://github.com/bulkaa87-cell/OpenFoamStudio.git
   cd OpenFoamStudio
   ```

3. Make a virtual environment in the folder `.venv`:

   ```
   python -m venv .venv
   ```

4. Install the packages:

   ```
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. Start the program with `OpenFOAM Studio.bat`. You can also start it from a console:

   ```
   .venv\Scripts\python.exe foam_studio.py [model.stl]
   ```

6. On the "Запуск" (Run) tab, select where OpenFOAM runs.

The program works without the Rust modules. Then it reads the fields and draws the "3D" tab with VTK.

## Build the Rust modules

1. Install Rust with `rustup`.
2. For `foamdist`, add the Linux target:

   ```
   rustup target add x86_64-unknown-linux-musl
   ```

3. Open `tools\build_rust.ps1`. The script sets `CARGO_HOME` and `RUSTUP_HOME` to fixed folders at the top. Change them to the folders of your Rust installation.
4. Run the script from the repository folder:

   ```
   powershell -ExecutionPolicy Bypass -File tools\build_rust.ps1
   ```

The script builds `foamrs.pyd`, `foamview.pyd` and `meshfix.pyd` and puts them in the repository folder.
It builds `foamdist` as a static Linux binary. The program copies this binary to the server when you start a distributed run.

## Known problems

- `meshfix` can make holes in some real models. A fix is in progress. Examine the result after each repair, or use "Полагодити як слайсер".
- `foamdist` is experimental. The author tested it on a small group of Linux machines on one local network only.

## Repository layout

| Path | Contents |
|---|---|
| `foam_studio.py` | The main window. Start the program with this file. |
| `fs_*.py` | The program modules: case writer, OpenFOAM backends, geometry, panels, views, results |
| `rust/foamrs/` | Fast field reading (Python module) |
| `rust/foamview/` | GPU view for the "3D" tab (Python module) |
| `rust/meshfix/` | Mesh repair (Python module, experimental) |
| `rust/foamdist/` | Distributed run on several Linux machines (Linux binary, experimental) |
| `tools/build_rust.ps1` | Build script for the Rust modules |
| `OpenFOAM Studio.bat` | Starts the program in `.venv` |

This repository holds only the program files.
The `.gitignore` file is an allow list. Notes, settings, cases, models and tests stay out.

## Contributing and security

- How to report a bug or propose a change: [CONTRIBUTING.md](CONTRIBUTING.md)
- How to report a vulnerability: [SECURITY.md](SECURITY.md)
- Rules for the community: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

OpenFOAM is a registered trademark of OpenCFD Ltd. This project is not approved or endorsed by OpenCFD Ltd.
