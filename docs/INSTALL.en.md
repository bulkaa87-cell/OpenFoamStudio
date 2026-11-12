# OpenFOAM Studio: install and server setup

This guide tells you how to install OpenFOAM Studio on Windows and how to connect a Linux server for the calculations. The names in the examples (`server1`, `alice`, `100.x.y.z`) are examples. Use your own names.

## 1. Install the program (Windows)

1. Download `OpenFOAM-Studio-<version>-windows-x64-setup.exe`.
2. Start the file. Administrator rights are not necessary: the installer puts the program in your user folder (`%LOCALAPPDATA%\Programs\OpenFOAM Studio`).
3. Select the language of the installer and the options:
   - **Desktop icon**: a shortcut on the desktop.
   - **Set up a calculation server after the install**: the program starts with the server wizard.
4. Click **Install**, then **Finish**.

The program keeps your files in these folders:

| What | Folder |
|---|---|
| Settings | `%APPDATA%\OpenFOAM Studio` |
| Cases, projects, the last session, saved meshes | `Documents\OpenFOAM Studio` |

The uninstaller (Windows Settings → Apps → OpenFOAM Studio) removes only the program. Your settings, cases and projects stay.

### OpenFOAM on this computer (optional)

OpenFOAM Studio runs OpenFOAM in WSL (Windows Subsystem for Linux), in Docker, or on a server. On the **Run** tab, click **Search** to find what this computer has. A server is the best choice for large meshes.

## 2. Connect a server: the wizard

Open **File → Set up a server…**. The wizard has these steps:

1. **Start**: select "Connect a server that is ready" or "Prepare a NEW, empty Linux server".
2. **Tailscale on this computer**: the wizard finds Tailscale and reads its state.
   - Tailscale is not installed: click **Download Tailscale** (the official page https://tailscale.com/download), install it, and log in. The page sees the change by itself.
   - Not logged in: click **Log in**. The login page opens in your browser. The program never asks for a password or an auth key.
3. **Setup script** (new server only): see section 3.
4. **Select the server**: select a computer of your tailnet, or a name from your SSH config, or type the address (`server1.tail1234.ts.net` or `100.x.y.z`). Type your user name on the server.
   - **Jump host**: fill it in only for a server that is not in the tailnet but is in the local network of another server. Example: lab computers `node1`…`node8` behind `server1`. Then SSH connects to `server1` first (ProxyJump).
5. **SSH key and login**:
   - No key yet: click **Make an SSH key**. The wizard makes `~/.ssh/id_ed25519` without a passphrase, because the program logs in without questions.
   - Click **Copy the key to the server**. A terminal window opens. Type the server password one time. The program does not see the password.
   - Click **Test the login**. The result must be "The key login works".
6. **Check the server**: the wizard reads the OpenFOAM install (native, Docker, Podman, version), the number of cores, the memory and the free disk. It changes nothing on the server.
7. Click **Finish**. The program uses the server for the runs (File → Settings → Machines). If necessary, the wizard adds a `Host` entry to `~/.ssh/config` and keeps a backup copy `config.bak-<time>`.

The same check by hand, in a terminal:

```
ssh alice@server1 "echo ok"
ssh alice@server1 "ls /usr/lib/openfoam/"
```

The program runs a case with the native OpenFOAM of the server (`/usr/lib/openfoam/openfoam2512`), or in a Docker or Podman image (for example `opencfd/openfoam-run:2512`). On the check page, select the install in **OpenFOAM for the runs**. With a container, each OpenFOAM command runs in `docker run` or `podman run` of the image, the same way as **Distribute between machines**. The case folders must be in `~/openfoam-studio` on the server (the program puts them there). With Docker, the account must be in the `docker` group.

## 3. Prepare a new, empty Linux server

The wizard makes a bash script for the server. Select the options:

- Install Tailscale (the official install script `https://tailscale.com/install.sh`) and start `tailscale up`.
- Install and start the SSH server.
- Add the SSH key of this computer to `~/.ssh/authorized_keys`. Then no password copy is necessary.
- OpenFOAM v2512: the package `openfoam2512-default` from openfoam.com (Ubuntu, Debian), or the Docker image `opencfd/openfoam-run:2512`.

Then do these steps:

1. Click **Save as file…** and save `ofs-server-setup.sh`.
2. Put the file on the server: a USB stick, `scp`, or paste the text into `nano ofs-server-setup.sh` in a terminal on the server.
3. On the server, log in as the account for the calculations and run:
   ```
   bash ofs-server-setup.sh
   ```
4. `tailscale up` prints a login address. Open it in a browser and log in with the same Tailscale account as on your computer.
5. At the end, the script prints the account name and the Tailscale name. Go back to the wizard, click **Next** and select the server.

The script uses `sudo` for the packages. You can run it again: it skips the parts that are already done. The program does not run the script anywhere.

## 4. Troubleshooting

### Tailscale shows "Connected", but SSH hangs

Tailscale can show "Connected" when the path to the server does not work. Two known causes:

- **An old relay (DERP)**. After sleep or after a change of the Wi-Fi network, this computer can keep an old relay of the server. Run `tailscale status`: the column after the address shows `direct` or `relay "<region>"`.
- **A public Wi-Fi** (for example the free Wi-Fi of a city or a train) can block the Tailscale traffic.

Do these steps, one at a time:

1. Try another network, for example the hotspot of your phone or the network of your university.
2. Run `tailscale netcheck` and look at the "Nearest DERP" line.
3. Restart the Tailscale service as administrator. In PowerShell (as administrator):
   ```
   Restart-Service Tailscale
   ```
   Or restart the computer.
4. Test again: `ssh alice@server1 "echo ok"`.

### "ping -l 1400" fails

This test tells nothing. The MTU (maximum packet size) of Tailscale is 1280 bytes. Thus `ping -f -l 1400 100.x.y.z` always fails, also on a good connection. The maximum value for this test is 1252.

### Other SSH errors

| Message | Cause | What to do |
|---|---|---|
| `Permission denied (publickey)` | The server does not have your key | Copy the key (wizard step 5) |
| `Could not resolve hostname` | The name is not known | Use the name from the Tailscale list or the `100.x.y.z` address. Make sure that MagicDNS is on |
| `Connection refused` | No SSH server runs | On the server: `sudo systemctl enable --now ssh` (Fedora: `sshd`) |
| `Connection timed out` | No path to the server | See "Tailscale shows Connected, but SSH hangs" |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The server key changed | If the server was installed again: `ssh-keygen -R server1`. If not, stop and ask the owner of the server |

## 5. macOS and Linux

The installer is for Windows. On macOS and Linux, run the program from the source folder:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python foam_studio.py
```

The server wizard works the same. On macOS and Linux, the wizard does not open a terminal for the key copy. It shows the command (`ssh-copy-id` or `cat … | ssh …`): paste it in a terminal.
