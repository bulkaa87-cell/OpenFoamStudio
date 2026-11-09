# Security policy

## Supported versions

Only the latest commit on the `main` branch gets security fixes. There are no release versions.

| Version | Supported |
|---|---|
| Latest `main` | Yes |
| Older commits | No |

## Report a vulnerability

Do not report a vulnerability in a public issue.

1. Open the "Security" tab of this repository.
2. Click "Report a vulnerability".
3. Write what the problem is and which file or function has the problem.
4. Write the steps that cause the problem.
5. Write what an attacker can do with the problem.
6. Send the report.

Only the repository owner can read the report. The owner replies in the same report.
This is a personal project. Thus, a reply can take some days.

## Risk areas

The program runs commands on your computer and on other computers. Examine these areas first:

- **ssh to servers.** The program uses the `ssh` command of your system with key login. It runs shell commands on the server in your user account. A server that you do not trust can get data about your cases. A case setting can become part of a shell command.
- **OpenFOAM commands.** The program writes OpenFOAM dictionaries and runs OpenFOAM programs in WSL, in Docker, in blueCFD-Core or on a server. A dictionary from a case that you did not make can run commands.
- **Coded functionObjects.** Some cases contain C++ code (coded functionObjects). OpenFOAM compiles this code on the server and runs it. Open a case from another person only if you trust that person.
- **Distributed runs (experimental).** `foamdist` uses an ssh key on the server to start containers on other machines. That key opens all these machines.
- **Downloaded programs.** The alpha wrap repair runs the external program `wrapwrap`. Download it only from its official release page.

## Out of scope

- Problems in OpenFOAM, Docker, Podman, WSL or ssh. Report these to their own projects.
- A wrong simulation result. Report it as a bug.
