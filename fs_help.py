"""Пояснення до кожного налаштування для режиму новачка.

Тексти тут — не переказ документації OpenFOAM, а відповідь на питання «що
станеться, якщо я це покручу». Де можливо, наведено числа з реальних
вимірів на цій самій програмі (див. devlog/ZNAHIDKY.md): вони корисніші за
загальні слова про «якість сітки».

Структура одного запису:
    what     — що це таке, одним реченням
    affects  — на що впливає далі по ланцюжку
    low      — що буде, якщо замало
    high     — що буде, якщо забагато
    typical  — робочий діапазон
"""

from __future__ import annotations

from dataclasses import dataclass
import fs_i18n
from fs_i18n import tr


@dataclass(frozen=True)
class Help:
    what: str
    affects: str = ""
    low: str = ""
    high: str = ""
    typical: str = ""

    def tooltip(self) -> str:
        parts = [tr(self.what)]
        if self.affects:
            parts.append(tr("Affects: {affects}", affects=self.affects))
        if self.typical:
            parts.append(tr("Typical: {typical}", typical=self.typical))
        return fs_i18n.join("\n\n", parts)

    def html(self) -> str:
        rows = ['<p style="margin:2px 0">' + tr(self.what) + "</p>"]
        if self.affects:
            rows.append(tr("<p style=\"margin:2px 0\"><b>Affects:</b> "
                           "{affects}</p>", affects=self.affects))
        if self.low:
            rows.append(tr("<p style=\"margin:2px 0\">↓ <i>too low:</i> {low}</p>", low=self.low))
        if self.high:
            rows.append(tr("<p style=\"margin:2px 0\">↑ <i>too high:</i> {high}</p>", high=self.high))
        if self.typical:
            rows.append(tr("<p style=\"margin:2px 0\"><b>Typical:</b> "
                           "{typical}</p>", typical=self.typical))
        return fs_i18n.join("", rows)


HELP: dict[str, Help] = {

    # ------------------------------------------------------------ геометрія
    "units": Help(
        what="The unit the STL coordinates are written in.",
        affects="Everything. OpenFOAM works in meters; an error here "
                "shifts the domain size, Re and the cell size by a "
                "factor of a thousand.",
        high="A model the size of a house: the domain is huge, billions "
             "of cells.",
        low="A model the size of a dust speck: a mesh of a few cells.",
        typical="“Auto” guesses from the overall size and is almost always right",
    ),
    "rotate": Help(
        what="Rotates ALL parts by 90° around the common center of the assembly — together, so the CAD assembly does not break.",
        affects="Flow direction. In this program the flow always goes "
                "along +X, span along Y, up is Z — it is the model that "
                "gets rotated.",
        typical="Aircraft: fuselage along X. Car: long axis along X",
    ),
    "repair_normals": Help(
        what="Turns all faces outward consistently.",
        affects="Prism layer growth and “inside/outside” checks. Where a "
                "normal disagrees with its neighbors, snappy does not "
                "know which way to grow the layer.",
        typical="Always press it when “Flipped faces” is not zero",
    ),
    "repair_weld": Help(
        what="Merges vertices closer than the set tolerance (like “Merge "
             "by Distance” in Blender).",
        affects="The number of open edges: gaps between parts mostly "
                "come from coincident vertices that are not stitched.",
        high="Destroys thin edges and small details.",
        typical="0.05–0.2 mm",
    ),
    "repair_decimate": Help(
        what="Reduces the number of triangles.",
        affects="The speed of everything: surface search in snappy and "
                "the response of the program itself.",
        high="Decimation tears thin walls and can open new holes.",
        typical="As many triangles as needed for an edge half the cell size",
    ),

    # ---------------------------------------------------------------- домен
    "upstream": Help(
        what="How many body lengths to leave in front of the body, up to the inlet.",
        affects="Pressure near the nose. An inlet that is too close “props up” the flow.",
        low="Overestimated stagnation pressure, distorted Cd.",
        high="Extra cells with no benefit.",
        typical="5 lengths",
    ),
    "downstream": Help(
        what="How many body lengths to leave behind the body, up to the outlet.",
        affects="The wake. If the outlet is close, it cuts across the "
                "vortices, and the run may not converge.",
        low="The wake hits the outlet, the residuals get stuck.",
        typical="12 lengths, 15 for bodies with a large span",
    ),
    "side": Help(
        what="Clearance to the side boundaries, in body lengths.",
        affects="Wall effect: a close boundary acts like a wind tunnel "
                "wall and increases the speed around the body.",
        low="Overestimated flow speed around the body, overestimated Cl.",
        typical="5 lengths",
    ),
    "base_cells_x": Help(
        what="How many cells the domain is divided into along the flow.",
        affects="The background cell size and, through the refinement "
                "level, also the cell size on the surface. The cell "
                "stays cubic; the division in Y and Z is calculated "
                "automatically.",
        low="Coarse background mesh: the transition to small cells near "
            "the body becomes abrupt.",
        high="Millions of cells in empty air.",
        typical="30–90",
    ),
    "ground": Help(
        what="Makes the bottom boundary of the domain a wall.",
        affects="Ground effect: a pressure build-up forms under the "
                "body, and lift increases. Required for a car, not for "
                "an aircraft in flight.",
        typical="Off, except for cases near the ground",
    ),

    # ---------------------------------------------------------------- сітка
    "level_max": Help(
        what="How many times the background cell is split in half near the body surface.",
        affects="The cell size on the surface and, through it, the shape "
                "detail, y+ and the cell count. Each level: the cell is "
                "half as big, and there are four times more cells near "
                "the surface.",
        low="The shape is blurred, flow separation is not captured.",
        high="The mesh will not fit in memory: level 9 instead of 5 gave "
             "92 M cells and 212 GB instead of 0.5 M and 1.1 GB.",
        typical="4–6",
    ),
    "level_min": Help(
        what="The minimum refinement level on the surface.",
        affects="How unevenly snappy can refine: between min and max it "
                "chooses by curvature on its own.",
        typical="One less than the maximum",
    ),
    "feature_level": Help(
        what="Refinement level on sharp edges.",
        affects="Edge sharpness: the leading edge of the wing, edges, joints.",
        low="Edges get rounded, and flow separation at them is not captured.",
        typical="The same as the maximum surface level",
    ),
    "resolve_feature_angle": Help(
        what="The angle between faces from which snappy treats a place "
             "as a “feature” and refines the mesh there.",
        affects="The cell count on rounded transitions.",
        low="Refinement turns on everywhere, the mesh bloats.",
        typical="30°",
    ),
    "included_angle": Help(
        what="The angle between adjacent triangles from which an edge "
             "counts as a sharp edge for surfaceFeatureExtract.",
        affects="Which edges snappy will try to reproduce as sharp.",
        low="On a faceted model, every seam between triangles becomes a "
            "“sharp edge”, and you get junk instead of sharp edges.",
        typical="150° for clean CAD, 170° for an assembly of parts",
    ),
    "cells_between_levels": Help(
        what="How many buffer-zone cells lie between adjacent refinement levels.",
        affects="Smoothness of the transition from large cells to small ones.",
        low="An abrupt size jump, poor cell quality at the interface.",
        high="Extra cells in the transition layers.",
        typical="3",
    ),
    "max_global_cells": Help(
        what="A hard cap on the cell count for snappy.",
        affects="A safeguard: when it reaches the limit, snappy stops "
                "refining instead of taking all the memory.",
        typical="As many as fit in memory",
    ),
    "box_factor": Help(
        what="How many times larger the refinement box is than the body size.",
        affects="The size of the region where the mesh is finer than the background mesh.",
        low="The refinement cuts off the boundary layer right next to the body.",
        typical="1.5–2",
    ),
    "box_wake": Help(
        what="How many body lengths the refinement box extends backward.",
        affects="Wake resolution. Without it, the wake smears out within "
                "a couple of lengths.",
        typical="3–5 lengths",
    ),
    "box_level": Help(
        what="Refinement level inside the refinement box.",
        affects="Mesh density in the volume around the body and in the wake.",
        high="Volume refinement costs more than surface refinement: each "
             "level means eight times more cells in the box.",
        typical="2",
    ),
    "les": Help(
        what="Eddy-resolving methods: LES resolves the large eddies "
             "instead of modeling all of them, DES does this only in the "
             "separated zone.",
        affects="Separation, stall, wake, flutter — everything where the "
                "flow is transient by nature and has no steady state.",
        low="RANS in separation gives a seesaw that does not converge: "
            "on a test wing at 10°, Cl swung from 0.31 to 0.77 and did "
            "not stop.",
        high="It is expensive: you need a transient run, a finer mesh "
             "and a smaller time step. For attached flow, RANS gives "
             "better value for the cost.",
        typical="kOmegaSSTIDDES for separation, kOmegaSST for everything else",
    ),
    "shell_on": Help(
        what="Refinement in a layer that follows the body shape, not in a box.",
        affects="Lift and drag accuracy with a smaller mesh. The region "
                "is set by the distance from the surface, so it does not "
                "touch the empty air above and below the body.",
        low="Without it, you have to get the near-wall resolution with a "
            "level over the whole surface, and that costs four times "
            "more than the same resolution in the shell.",
        typical="On for a wing and thin bodies",
    ),
    "shell_reach": Help(
        what="How far from the surface the outer step of the shell extends.",
        affects="The thickness of the refined region. Inside it, the "
                "level increases step by step toward the wall.",
        low="A step thinner than a few surface cells does not catch any cells.",
        typical="0 — calculate from the surface cell",
    ),
    "shell_level": Help(
        what="Refinement level right at the wall, inside the shell.",
        affects="Resolution where the suction peak sits — a few "
                "millimeters from the leading edge.",
        typical="0 — one above the surface level",
    ),
    "shell_steps": Help(
        what="How many steps to split the transition from the wall to "
             "the background mesh into.",
        affects="Smoothness of the transition. At an abrupt level jump, "
                "the solution reflects.",
        low="One step means a jump of several levels at once.",
        typical="3",
    ),
    "layers_on": Help(
        what="Grow thin prism cells near the wall.",
        affects="Friction drag. Without layers, the first cell equals "
                "the surface cell, y+ ends up in the hundreds, and the "
                "shear stresses are unreliable.",
        typical="On if you need drag, not only pressure",
    ),
    "n_layers": Help(
        what="How many layers to grow.",
        affects="The first layer thickness and, through it, y+.",
        high="This is not intuitive: the more you ask for, the FEWER "
             "faces get them. Measured on a real model: 3 layers — 79% "
             "of faces, 5 layers — 65%, 10 layers — 37%. Patchy layers "
             "are worse than even thin ones.",
        typical="3–5 on assemblies, up to 15 on clean geometry",
    ),
    "expansion": Help(
        what="How many times thicker each next layer is than the previous one.",
        affects="How fast the layer stack grows from thin at the wall to "
                "the size of a normal cell.",
        high="An abrupt size jump between layers degrades quality.",
        typical="1.15–1.2",
    ),
    "final_layer": Help(
        what="Thickness of the last layer as a fraction of the surface cell.",
        affects="The total stack thickness. This is the main lever when "
                "y+ and the boundary layer thickness conflict: a thinner "
                "last layer makes the whole stack thinner without "
                "sacrificing a thin first layer.",
        typical="0.3–0.5; the assistant lowers it to 0.15 when y+ cannot be reached otherwise",
    ),
    "min_thickness": Help(
        what="The lower limit of layer thickness below which snappy does not grow the layer.",
        affects="Whether layers will grow in narrow places.",
        high="In gaps, layers will not grow at all.",
        typical="0.05",
    ),
    "target_yplus": Help(
        what="The target dimensionless distance to the center of the first cell.",
        affects="The number of layers and the first layer thickness. The "
                "required y+ is set by the turbulence model. kOmegaSSTLM "
                "(transition) and LES resolve the layer down to the "
                "wall: y+ ≈ 1, no more than 1–2. kOmegaSST, "
                "SpalartAllmaras and kEpsilon with the law of the wall: "
                "30–150; for kEpsilon a finer wall will not help. The "
                "“Wall treatment” list: by default Spalding (any y+), "
                "standard — only 30–300, Low-Re — only ≈ 1.",
        low="y+ around 1 means resolving the viscous sublayer: you need "
            "dozens of layers and a flawless STL.",
        high="Above 300 — the law of the wall no longer holds, friction is unreliable.",
        typical="“Auto” chooses for the model and Re. Do NOT aim at "
                "5–30: this is the buffer layer, the worst zone for wall "
                "functions",
    ),

    # ---------------------------------------------------------------- потік
    "u_inf": Help(
        what="Freestream speed.",
        affects="Re, y+, the boundary layer thickness and the stability "
                "of the run. The default solver settings come from a "
                "tutorial at 20 m/s; at 85 m/s the same relaxation is "
                "four times more aggressive.",
        high="Above ~100 m/s (M > 0.3), compressibility is already "
             "noticeable, but simpleFoam does not see it.",
        typical="The one you are interested in; above 40 m/s turn on the stable start",
    ),
    "aoa": Help(
        what="Angle of attack: the inclination of the freestream in the vertical plane.",
        affects="Lift. Set by rotating the flow, not the geometry — the "
                "Cl and Cd directions are updated automatically.",
        high="Beyond the stall angle, RANS greatly overestimates lift.",
        typical="From −5° to 15° for a wing",
    ),
    "aos": Help(
        what="Sideslip angle: the inclination of the flow in the horizontal plane.",
        affects="Side force and yawing moment.",
        typical="0, except for crosswind cases",
    ),
    "nu": Help(
        what="Kinematic viscosity of the medium.",
        affects="Re and the whole boundary layer. For air 1.47e-5, for "
                "water about 15 times less.",
        typical="Choose a ready-made preset instead of typing it in by hand",
    ),
    "rho": Help(
        what="Density of the medium.",
        affects="Only the conversion of forces into coefficients: "
                "simpleFoam is incompressible and has no density in its "
                "equations at all.",
        typical="1.225 for air at sea level",
    ),
    "split_patches": Help(
        what="Whether to make a separate boundary from each model.",
        affects="Forces: with separate patches, Cd and Cl are also "
                "calculated separately for each part, not only in total. "
                "The totals stay.",
        typical="off, until you need to know the contribution of the "
                "wing or the engine nacelle specifically",
    ),
    # ------------------------------------------------------------ двигуни
    "engine_name": Help(
        what="The name of the engine that this cap belongs to.",
        affects="Caps with the same name are one engine: the air that "
                "goes into its intakes comes out of its nozzle.",
        typical="engine1; for the second engine — engine2",
    ),
    "engine_kind": Help(
        what="Impeller (EDF) or turbine.",
        affects="Only the jet: for a turbine it is hot, so it is "
                "lighter, and at the same thrust it comes out faster. A "
                "hot jet makes sense only in compressible mode.",
        typical="EDF for model aircraft, turbine — for jets",
    ),
    "engine_thrust_mode": Help(
        what="What exactly the number in the “Thrust” field means.",
        affects="“Maximum static” — the datasheet number: thrust on the "
                "test stand at full throttle. From it, the program finds "
                "the engine flow — the mass flow and the jet speed — and "
                "the throttle scales the flow itself, like rpm. Thrust "
                "in flight then COMES OUT on its own and drops with "
                "speed. “In flight” — you already know the thrust at "
                "this speed, and the throttle only multiplies it by "
                "throttle².",
        typical="maximum static — if the number is from the datasheet",
    ),
    "engine_thrust": Help(
        what="Thrust at full throttle: static from the datasheet or in "
             "flight — as selected above.",
        affects="The mass flow and the jet speed: the program calculates "
                "them from the thrust and the nozzle area.",
        high="If M > 0.9 at the nozzle exit, the nozzle is choked, and "
             "the model with atmospheric pressure at the exit no longer "
             "works.",
        typical="static — as in the datasheet; in flight for an EDF "
                "usually 70–90 % of static at cruise",
    ),
    "engine_vjet": Help(
        what="Exhaust gas speed from the datasheet, at full throttle.",
        affects="The mass flow: it is then taken from the thrust (ṁ = F "
                "/ Vₑ), not from the nozzle area, and you can draw the "
                "portal disk at any size. Without it, the speed comes "
                "from the area — and a cap that is too large gives a "
                "slow jet.",
        low="“From nozzle area” — the speed is calculated from the thrust and the cap area.",
        typical="micro turbojet 400–600 m/s; EDF 50–150 m/s",
    ),
    "engine_tjet": Help(
        what="Jet temperature at the nozzle exit.",
        affects="The jet density and, with it, the jet speed at the same "
                "thrust.",
        typical="500–700 °C for model turbines",
    ),
    "engine_fuel": Help(
        what="By how much the jet mass is greater than the mass of the air at the intake.",
        affects="The difference in mass flow between the nozzle and the intake.",
        typical="0 for EDF, about 2 % for a turbine",
    ),
    "engine_throttle": Help(
        what="Whether the throttle on the control panel scales the thrust.",
        affects="Throttle is a fraction of rpm, so thrust goes as "
                "throttle²: half throttle — a quarter of the thrust.",
        typical="on",
    ),
    "engine_refine": Help(
        what="Whether the program refines the mesh on the caps automatically.",
        affects="So that at least ten cells fit across the cap diameter. "
                "Thrust does not depend on this (it is set by the mass "
                "flow), but the jet pattern does.",
        typical="on",
    ),
    "engine_direct": Help(
        what="Set the mass flow and the jet speed directly, not the thrust.",
        affects="Thrust is then only shown: F = ṁ·Vₑ − ṁ_in·V∞.",
        typical="off, if the datasheet gives thrust",
    ),
    # ------------------------------------------------------------ гвинт-диск
    "disk_thrust": Help(
        what="Propeller thrust at full throttle at this flight speed.",
        affects="The force with which the disk pushes air along the "
                "axis. It is exactly this value, however the disk falls "
                "on the cells: the distribution is normalized on the "
                "mesh itself.",
        typical="from the propeller table or a test stand measurement, "
                "minus the drop with speed",
    ),
    "disk_torque": Help(
        what="The torque with which the propeller swirls the jet. Zero — "
             "no swirl.",
        affects="The swirl of the slipstream behind the propeller — and "
                "with it, asymmetric airflow over the tail and the wing. "
                "The swirl direction is the sign of the rpm.",
        high="Q/(T·R) above 2 — most likely the units are wrong (N·mm instead of N·m).",
        typical="Q ≈ P/ω: shaft power divided by the rotation speed in rad/s",
    ),
    "disk_profile": Help(
        what="How the thrust is distributed along the radius.",
        affects="Where exactly the propeller blows. A uniform one also "
                "blows near the spinner, where the fuselage usually is, "
                "so it overestimates the airflow over the nose.",
        typical="realistic: zero at the spinner and at the tip, maximum "
                "closer to the blade tip",
    ),
    "disk_hub": Help(
        what="Spinner radius — the center, where there is no force.",
        typical="15 % of the propeller radius, if not set",
    ),
    "disk_refine": Help(
        what="Whether the program refines the mesh in the disk automatically.",
        affects="So that at least twenty cells fit across the diameter. "
                "Thrust does not depend on this, but the jet shape does.",
        typical="on",
    ),
    "porous": Help(
        what="A box inside which the flow slows down, as through an obstacle.",
        affects="This is how radiators, inlet screens, filters and "
                "parachutes are calculated: there is no point in "
                "modeling each hole, and the losses are reproduced "
                "accurately.",
        typical="off, until the model has such an obstacle",
    ),
    "porous_dp": Help(
        what="How many pascals are lost at the obstacle.",
        affects="How strongly it slows the flow. This is the number in "
                "the datasheet of the radiator or screen — the program "
                "calculates the Darcy coefficients from it on its own.",
        typical="100–500 Pa for a radiator, 20–100 Pa for a protective screen",
    ),
    "rotating": Help(
        what="A cylinder inside which the air is calculated in a "
             "rotating reference frame (MRF).",
        affects="This is how propellers and rotors are calculated "
                "without moving the mesh. Thrust comes not from the zone "
                "but from the blades INSIDE it: an empty zone only "
                "swirls the air, and this looks like “the propeller does "
                "not pull”.",
        typical="off, until the model has a propeller",
    ),
    "rpm": Help(
        what="Propeller revolutions per minute. The sign sets the direction of rotation.",
        affects="Thrust and wake swirl. Also the blade tip speed: it "
                "easily exceeds the speed of sound even when the "
                "aircraft itself flies slowly, and then the "
                "incompressible setup gives nonsense.",
        typical="1500–8000 for a UAV propeller; the program shows M at the blade tip",
    ),
    "steady": Help(
        what="Look for one steady state or calculate the development in time.",
        affects="The solver (simpleFoam versus pimpleFoam), the meaning "
                "of “how long to run” — iterations or seconds, and "
                "whether there will be convergence at all: a transient "
                "run does not converge, it just goes on.",
        typical="steady almost always; transient — when you need stall, "
                "a Kármán vortex street or unsteady loads",
    ),
    "end_time": Help(
        what="How many seconds of physical time to calculate.",
        affects="The run duration, directly: together with the time step "
                "it gives the number of steps.",
        typical="at least three domain flow-throughs — otherwise the "
                "wake behind the body does not have time to form",
    ),
    "warm_iterations": Help(
        what="A compressible transient run first runs this many "
             "iterations of steady rhoSimpleFoam, and its field becomes "
             "time 0.",
        affects="Without it, the run starts with “all the air is already "
                "flying” — near walls and in ducts this is a water "
                "hammer: a shock wave, a temperature crash, FPE in the "
                "first steps. The cost is a few minutes before the "
                "transient run. Zero turns it off.",
        typical="300; for a body with internal ducts (EDF, intake) do "
                "not turn it off",
    ),
    "courant": Help(
        what="The Courant number cap: how many cells the flow passes in "
             "one step.",
        affects="The time step is chosen automatically so as not to "
                "exceed it. Higher is faster, but less accurate and less "
                "stable.",
        typical="3–10 with two outer passes; less than 1 if there is one pass",
    ),
    "regime": Help(
        what="Incompressible or compressible flow.",
        affects="Which solver goes into the case: simpleFoam or "
                "rhoSimpleFoam. In compressible mode, temperature and "
                "energy are added, and density and viscosity are no "
                "longer settings — they are calculated from pressure and "
                "temperature.",
        typical="incompressible up to M = 0.3; above it, drag is underestimated",
    ),
    "turbulence": Help(
        what="Turbulence model.",
        affects="How mixing and flow separation are calculated.",
        typical="kOmegaSST is more accurate in separated regimes; "
                "SpalartAllmaras is cheaper and classic for cruise "
                "regimes without large separation; kOmegaSSTLM also "
                "calculates the laminar-to-turbulent transition, but "
                "only when the boundary layer is resolved down to the "
                "wall (y+ ≈ 1)",
    ),
    "intensity": Help(
        what="Freestream turbulence level.",
        affects="Initial k and omega. Low in the free atmosphere, higher "
                "in a wind tunnel.",
        typical="0.5–1% for free flight",
    ),
    "visc_ratio": Help(
        what="Ratio of turbulent viscosity to molecular viscosity at the inlet.",
        affects="How fast turbulence decays from the inlet to the body.",
        high="Overestimated background viscosity “smears” the wake.",
        typical="1–10",
    ),
    "aref": Help(
        what="The area to which Cd and Cl are normalized.",
        affects="Directly divides the coefficients. An error of 2.5 "
                "times here gives an error of 2.5 times in Cd — that is "
                "exactly what happened when the bounding planform was "
                "used instead of the real silhouette.",
        typical="For a winged vehicle the planform area, for a compact "
                "body the frontal area. The button takes the real "
                "silhouette of the model",
    ),
    "lref": Help(
        what="Reference length.",
        affects="Re and the pitching moment. Does not affect Cd and Cl.",
        typical="Chord for a wing, length for a fuselage",
    ),
    "farfield": Help(
        what="Which type of conditions to set on the outer boundaries of the domain.",
        affects="Whether the flow can enter at an angle. freestream "
                "determines on its own where the inlet is and where the "
                "outlet is. A wind tunnel with slip cuts off the cross "
                "component: on a wing at 5°, this lowered Cl from 0.243 "
                "to 0.035.",
        typical="freestream always, except for zero angles",
    ),

    # ---------------------------------------------------------- розвʼязувач
    "iterations": Help(
        what="Cap on the number of iterations.",
        affects="Run time. The run stops earlier as soon as the "
                "residuals drop below the threshold.",
        low="Stops before convergence; the coefficients still drift.",
        typical="1000–2000",
    ),
    "write_interval": Help(
        what="How often to save the fields to disk.",
        affects="Disk space and how many intermediate states you can view.",
        low="Tens of gigabytes of time directories.",
        typical="Equal to the number of iterations if you only need the result",
    ),
    "residual_tol": Help(
        what="The threshold below which the run counts as converged and stops.",
        affects="When the run ends.",
        high="Stops too early; the coefficients have not settled yet.",
        typical="1e-4. But look at the Cd scatter, not at the residuals",
    ),
    "procs": Help(
        what="How many processes to split the job into.",
        affects="Speed. Each process computes its own part of the mesh.",
        high="Fewer than 40 thousand cells per process — the exchange "
             "between subdomains eats up all the gain.",
        typical="Number of cells divided by 40 thousand, but not more "
                "than the number of cores minus two",
    ),
    "decompose": Help(
        what="How to split the mesh between processes.",
        affects="Load balance and the amount of exchange.",
        typical="scotch — finds the split with the smallest boundary by itself",
    ),
    "potential_init": Help(
        what="Compute a potential flow before the main run.",
        affects="Initial field: instead of a uniform flow, the solver "
                "starts from something that is already close to the "
                "truth.",
        typical="On — costs seconds, and convergence is noticeably better",
    ),
    "consistent": Help(
        what="SIMPLEC instead of the classic SIMPLE.",
        affects="Convergence speed. Allows high relaxation without "
                "separate pressure relaxation.",
        high="On a dirty mesh or at high speed, this is exactly what "
             "makes the run diverge.",
        typical="On; if the run diverges, the robust start helps",
    ),
    "check_mesh": Help(
        what="Run a mesh quality check after meshing.",
        affects="Whether you learn about skewed cells before the solver "
                "diverges on them.",
        typical="On, costs seconds",
    ),
    "stop_on_bad_mesh": Help(
        what="Stop the run if the mesh is unusable.",
        affects="The stop triggers only on really bad cases: skewness "
                "from 8, non-orthogonality from 75, or negative volumes. "
                "Almost all real meshes have a few boundary faces with "
                "skewness 4–5, and these do not stop the run.",
        typical="On",
    ),
    "robust_start": Help(
        what="Compute the WHOLE run robustly: first-order schemes, low "
             "relaxation, a speed cap.",
        affects="Stability at the cost of accuracy: upwind smears the solution.",
        typical="Off. Use the two-stage start instead",
    ),
    "robust_iterations": Help(
        what="How many first iterations to compute robustly before the "
             "switch to the accurate schemes. Zero turns off the second "
             "stage.",
        affects="Whether the run survives the first tens of iterations. "
                "After that it CONTINUES from the field it got, so you "
                "get both a stable start and an accurate result in one "
                "pass.",
        low="At high speed it diverges before the switch.",
        typical="150 up to 40 m/s, 300 up to 90, 500 above",
    ),
    "velocity_cap": Help(
        what="Cap on the velocity magnitude in the first stage, as a "
             "fraction of the freestream.",
        affects="Keeps a local spike in one bad cell from growing and "
                "blowing up the run. On a converged field it does "
                "nothing.",
        low="Clips the real flow acceleration around the body.",
        typical="1.5",
    ),

    # ----------------------------------------------------------- результати
    # Один блок на весь ряд шарів: чотири окремі стояли б стовпчиком над
    # вʼюпортом і стискали його вдвічі. Подробиці — у підказці кожної галочки.
    "layers": Help(
        what="Layers over the body — each answers one question. Reverse "
             "flow: where the separation is. Wake: where the drag work "
             "goes. Lines on the body: where the separation line is. "
             "Propeller wash: which parts the slipstream reaches.",
        affects="Below the view — numbers for each: reverse-flow volume, "
                "drag summed from the faces and its Cd, fractions of the "
                "wash lines.",
        typical="Hover the mouse over the check box for details",
    ),
    "layer_reverse": Help(
        what="Red semi-transparent bubbles where the air in the volume "
             "goes BACKWARD (against the flow, faster than 2% of U∞). "
             "This is the separated zone itself, not a sign of it.",
        affects="Shows where the separation is and how large it is: the "
                "line below gives the volume of air that goes backward. "
                "On the reference wing gui_wing (5°) it is 144 cm³ over "
                "the rear three quarters of the chord.",
        typical="None at all — the flow stays attached; spots behind the "
                "nose, cockpit, nacelles — local bubbles; a continuous "
                "band over the wing — stall",
    ),
    "layer_loss": Help(
        what="Light-blue wake shell: where the air lost more than 10% of "
             "the dynamic pressure. Total pressure is the energy that "
             "the air carries; all the work of the drag force goes into "
             "exactly these losses.",
        affects="Shows WHAT makes the drag: a wide wake behind a nacelle "
                "or the tail is the place worth smoothing out. Near the "
                "wall itself the shell is removed (that is the boundary "
                "layer, it is everywhere).",
        typical="A thin wake behind the trailing edge and two tip "
                "vortices — good; thick “sleeves” behind add-ons — bad",
    ),
    "layer_wall": Help(
        what="Dark lines on the surface itself — where the air runs near "
             "the wall, like oil and soot on a model in a wind tunnel. "
             "Built from the wall friction field wallShearStress.",
        affects="Lines converge into one — that is a separation line; "
                "they diverge — reattachment. They curl near the wing "
                "tip — a tip vortex.",
        typical="Needs the wallShearStress field at the time step: the "
                "program turns it on in every case, but old cases may "
                "not have it",
    ),
    "layer_wash": Help(
        what="Lines from the propeller disks, colored by the body part "
             "that they already touched: gray — none yet, then the color "
             "of the part.",
        affects="Answers “does the wash reach the tail / into the duct / "
                "over the top of the wing”. Below the view — the "
                "fraction of lines that reached each part, and from "
                "which side (top, bottom, side). Parts are the separate "
                "patches of the body; if there is one body, it is split "
                "into thirds along the flow.",
        typical="Needs propellers in the case (rotating zones)",
    ),
    "streams_seed": Help(
        what="Where the streamlines start from. “Propellers and ahead of "
             "the nose” — rings in front of the disks and a rake in "
             "front of the body. “Segment” and “Sphere” stay in the "
             "window: drag them with the mouse, and the lines are "
             "rebuilt when you release the button. “On the slice plane” "
             "— a grid of seeds on the plane that the slice bar sets.",
        affects="Display only: it does not affect the computation. A "
                "segment across the wing tip shows the tip vortex, a "
                "sphere behind the tail shows the wake. In the middle of "
                "the flow, use “Both directions”: otherwise you only see "
                "where the air goes, not where it came from.",
        low="Too few lines — a vortex or separation slips between them unnoticed.",
        high="A thousand lines take tens of seconds to compute and merge into a band.",
        typical="75 lines; for the sphere — 100–300",
    ),
    "slice_arrows": Help(
        what="Velocity arrows in the slice plane, one per cell of a "
             "uniform grid. Length is speed; the scale stays fixed while "
             "you move the plane.",
        affects="Red arrows — the air goes against the flow: this way "
                "you see separation directly on the slice. On a plane "
                "across the flow, the arrows show swirl and downwash.",
        typical="The slice caption tells how many m/s an arrow one cell "
                "long stands for",
    ),
}


def get(key: str) -> Help | None:
    return HELP.get(key)
