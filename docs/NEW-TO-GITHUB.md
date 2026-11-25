# New to GitHub? Start here (Windows, step by step)

This page is for you if you have not installed much from GitHub before, or if the
install stopped with an error you do not understand. It assumes nothing. Every step
says what to type, what you should see, and what to do when you see something else.

You do not need to run PowerShell as administrator for any of this.

## How the pieces fit together

Sextant is not an app you download and double-click. It is a set of program files, and
three things work together to get it running:

1. **GitHub holds the software.** The files sit on a website, free for anyone to read
   or copy.
2. **pip fetches it.** One command copies those files from GitHub onto your computer,
   along with the other pieces they depend on. (Git is one way pip can fetch them. There
   is also a way that needs no Git.)
3. **Python runs it.** The files are written in the Python language, so Python has to
   be on your computer to carry out what they say.

After that, you connect Sextant to the AI program you already use, and it becomes that
program's memory. So the whole job is: have Python, fetch Sextant, connect it. The
steps below do exactly that, in that order.

## What the words mean

Most install pages assume you know these. Here they are in one line each.

- **GitHub**: a website where people keep and share the files of a software project.
  Sextant's files live at https://github.com/protechtus/sextant.
- **Git**: a program on your computer that downloads a project from GitHub and keeps it
  up to date. GitHub is the website, Git is the tool. You can install Sextant without it.
- **Python**: the programming language Sextant is written in. Your computer needs
  Python installed to run it, the same way it needs a PDF reader to open a PDF.
- **pip**: Python's installer. It comes with Python. `pip install <something>` downloads
  a Python program and everything that program needs.
- **PowerShell**: the window where you type commands on Windows. Type a line, press
  Enter, read what comes back.
- **PATH**: the list of folders Windows searches when you type a program's name. "Not
  recognized" or "not in your PATH" means the program is missing, or it is installed in a
  folder that is not on that list. There is a section on fixing that below.
- **venv** (virtual environment): a private copy of Python inside one folder, so what
  you install for Sextant cannot disturb anything else.
- **MCP client**: the AI program Sextant plugs into, for example Claude Code. Sextant
  is the memory, the client is what you talk to.

If you use an AI coding agent already, the easier road is to tell it: *"Read
https://usesextant.com/agent-install.md and follow it."* This page is the by-hand road.

## The one rule that fixes most problems

**After you install anything, close the PowerShell window and open a new one.**

A PowerShell window only knows about the programs that existed when it was opened. If
you install Git and then type `git` in the same window, Windows still says it cannot
find it. That is not a failed install. Open a new window and try again.

If you would rather not close the window, this one line makes the current window
re-read the list of installed programs. Paste it, press Enter, then try your command
again:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
```

## Step 1: check the three things Sextant needs

Open PowerShell (Start menu, type `powershell`, press Enter). Type each line and press
Enter.

**Python, 3.11 or newer**

```powershell
py --list
```

Good: a list that includes 3.11, 3.12, 3.13 or newer.
Not good: "not recognized", or only older numbers. Install it one of two ways, then
open a new window. Both give the same result. Pick whichever feels more familiar.

- **With an installer (download and click).** Go to https://www.python.org/downloads/,
  press the big download button, and open the file it downloads. If the first screen
  shows a box named "Add python.exe to PATH", tick it. Then click Install Now.
- **With one command.**

  ```powershell
  winget install Python.Python.3.12
  ```

**An AI client that Sextant can connect to** (this page uses Claude Code)

```powershell
claude --version
```

Good: a version number. Not good: "not recognized". Install Claude Code from
https://claude.com/product/claude-code, then open a new window.

**Git** (optional for this page, needed if you want to update with one command later)

```powershell
git --version
```

Good: `git version 2.something`. Not good: "not recognized". Either skip Git and use
the "without Git" line in step 3, or install it one of two ways and open a new window:

- **With an installer (download and click).** Go to https://git-scm.com/download/win
  and open the file it downloads. It asks many questions. Keep clicking Next: every
  default answer is fine. One screen is titled "Adjusting your PATH environment". Leave
  it on the recommended middle choice, "Git from the command line and also from
  3rd-party software". That choice is what puts Git on the PATH.
- **With one command.**

  ```powershell
  winget install Git.Git
  ```

What you may see from the command, and what it means:

- A download bar, then `Successfully installed`: Git is installed. Open a new window
  and type `git --version`.
- `Found an existing package already installed` or `No available upgrade found`: Git
  is ALREADY on this computer, and winget has nothing to do. The install is not the
  problem. This window just cannot find Git. Open a new window and try
  `git --version`.
- A new window still says "not recognized": Git is installed but its folder is not on
  the PATH. Go to [Putting Git on the PATH](#putting-git-on-the-path) further down.
  It is three short steps.

Git worked before and stopped? The cause is the same: this window cannot find it.
The same three answers apply.

If `winget` itself is "not recognized", use the installer way for each one.

## Step 2: make a folder and a private Python for Sextant

```powershell
cd $HOME
mkdir sextant-install
cd sextant-install
py -3 -m venv .venv
```

The last line makes a folder named `.venv`. It is a private copy of Python, so nothing
you install here can disturb anything else on the computer. It prints nothing when it
works.

## Step 3: install Sextant

Without Git (works on any machine):

```powershell
.\.venv\Scripts\python.exe -m pip install https://github.com/protechtus/sextant/archive/refs/heads/main.zip
```

With Git:

```powershell
.\.venv\Scripts\python.exe -m pip install git+https://github.com/protechtus/sextant
```

Both install the same thing. It takes a minute or two and prints a lot. Good: the last
lines include `Successfully installed` and `sextant-ai`.

pip stops there. It does not tell you what to do next, so here it is. Type:

```powershell
.\.venv\Scripts\sextant.exe
```

Good: a short summary that starts with `Sextant v0.1.0` (or newer) and lists your next
three commands. Step 4 walks through them.

## Step 4: set it up and connect it

```powershell
.\.venv\Scripts\sextant.exe init
.\.venv\Scripts\sextant.exe setup --client claude-code
```

`init` makes the folder your notes live in (`.memory` in your user folder) and lists
what it created. `setup` prints one line that starts with `claude mcp add`. Copy that
whole line, paste it into PowerShell and press Enter. That is what connects Claude Code
to Sextant. Then:

```powershell
.\.venv\Scripts\sextant.exe verify
```

Good: the last line says `ROUND-TRIP: OK`. A test note went in and came back. You are
done. The next places to read are [GETTING-STARTED.md](GETTING-STARTED.md) and
[USAGE.md](USAGE.md).

## Putting Git on the PATH

The Git installer normally does this for you, and a new PowerShell window is all it
takes. Do this section only if a NEW window still says `git` is not recognized.

1. Find out where Git was installed. One of these prints `True`:

   ```powershell
   Test-Path "C:\Program Files\Git\cmd\git.exe"
   Test-Path "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
   ```

   If both print `False`, Git is not installed: go back to step 1.

2. Add that `cmd` folder to your PATH. There are two ways, and they do the same thing.
   Pick one.

   **By clicking.** Press the Start button, type `environment`, and choose "Edit
   environment variables for your account". In the top list click `Path`, then Edit,
   then New, and paste the folder that printed `True`, without the `\git.exe` at the
   end (for example `C:\Program Files\Git\cmd`). Click OK twice.

   **By command.** Paste these two lines into PowerShell, one at a time. Change the
   folder in the first line if yours was the other one.

   ```powershell
   $folder = "C:\Program Files\Git\cmd"
   [Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $folder, "User")
   ```

   The second line reads your own PATH list, adds the folder to the end, and saves it.
   It changes only your account, so it needs no administrator rights, and it prints
   nothing when it works.

   You may see `setx PATH "%PATH%;..."` recommended elsewhere. Avoid it for PATH: it
   copies the whole system list into your personal one and cuts the result off at 1,024
   characters, which can silently drop folders. `setx` is fine for ordinary settings
   (`setx NAME value`), just not for adding to PATH.

3. Close PowerShell, open a new window, and type `git --version`.

The same three steps work for any program that is installed but "not recognized".

### You added it and it is STILL not recognized

This happens on real machines, most often ones that have been used for years. The
entry is in the list, the program is on the disk, and Windows still cannot find it. The
usual cause is something you cannot see: a space or a line break stuck to the end of
the entry, often from a paste. Windows then looks for a folder whose name ends in that
invisible character, and no such folder exists. (An entry that ends in a space or a
line break fails in both PowerShell and the old Command Prompt. An extra `;` at the end
is harmless.)

First, look. This writes a short report to your Desktop without changing anything:

```powershell
& { "GIT.EXE FOUND AT:"; Get-ChildItem "C:\Program Files\Git","C:\Program Files (x86)\Git","$env:LOCALAPPDATA\Programs\Git" -Filter git.exe -Recurse -ErrorAction SilentlyContinue | ForEach-Object FullName; "--- PATH IN THIS WINDOW:"; $env:Path -split ';' | ForEach-Object { "[$_]" }; "--- SAVED USER PATH:"; [Environment]::GetEnvironmentVariable('Path','User') -split ';' | ForEach-Object { "[$_]" } } *> "$HOME\Desktop\path-check.txt"
```

Open `path-check.txt`. Every entry is shown inside square brackets, so anything extra
shows up: `[C:\Program Files\Git\cmd ]` has a space before the closing bracket, and an
entry with a line break is split across two lines. A healthy entry looks like
`[C:\Program Files\Git\cmd]`.

Then, fix. This line rewrites your own PATH list with every entry trimmed and the
empty ones dropped. It changes nothing else, and it does not need administrator rights:

```powershell
[Environment]::SetEnvironmentVariable("Path", (([Environment]::GetEnvironmentVariable("Path", "User") -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join ';'), "User")
```

Open a new window and try `git --version`. If the report shows the broken entry under
"PATH IN THIS WINDOW" but not under "SAVED USER PATH", it lives in the system-wide list,
which needs an administrator to edit: in the Environment Variables window, fix it in
the lower list ("System variables") by deleting the entry and typing it again by hand.

When you add an entry yourself, type it or paste it, then check there is nothing after
the last letter.

## When you see an error

Find your message in the left column. The words do not have to match exactly.

| What the screen says | What it means | What to do |
| --- | --- | --- |
| `ERROR: [WinError 2] The system cannot find the file specified while executing command git version` or `Cannot find command 'git' - do you have 'git' installed and in your PATH?` | The install line you used needs Git, and this window cannot find Git. | Use the "without Git" line in step 3. Or install Git (step 1), **open a new PowerShell window**, `cd $HOME\sextant-install`, and run the line again. |
| You installed Git and it still says it cannot find `git` | The window is older than the install. | Close PowerShell, open a new one, type `git --version`. If a new window still fails, see "Putting Git on the PATH" above. |
| `winget install` says `Found an existing package already installed` or `No available upgrade found`, and the program is still "not recognized" | The program is installed. The window cannot find it. | Open a new window. If that fails too, add its folder to the PATH: see "Putting Git on the PATH" above. |
| `[notice] A new release of pip is available` | Not an error. pip is telling you about its own update. | Nothing. You can ignore it, or run the command it shows. Either way, the install you asked for still has to be run. |
| `py : The term 'py' is not recognized` | Python is not installed, or the window is older than the install. | Step 1, Python. Then a new window. |
| `No suitable Python runtime found` | Python is installed, but not the version the line asked for. | Use `py -3 -m venv .venv` (any version), after `py --list` shows 3.11 or newer. |
| `requires a different Python: 3.10.x not in '>=3.11'` | The `.venv` was made with an older Python. | Delete the `.venv` folder, install Python 3.12 (step 1), new window, repeat step 2. |
| `running scripts is disabled on this system` | You tried to "activate" the `.venv`, and Windows blocks scripts by default. | You do not need to activate. Use the full `.\.venv\Scripts\...` lines on this page. |
| `sextant : The term 'sextant' is not recognized` | You typed `sextant` alone. That only works in an activated `.venv`. | Type `.\.venv\Scripts\sextant.exe` instead, from inside the `sextant-install` folder. |
| `The term '.\.venv\Scripts\...' is not recognized` on a `.\.venv\...` line (in the old Command Prompt: `The system cannot find the path specified`) | You are not in the folder from step 2. | `cd $HOME\sextant-install`, then run the line again. |
| `claude : The term 'claude' is not recognized` | Claude Code is not installed, or the window is older than the install. | Step 1, AI client. Then a new window. |
| You ran `pip install sextant` | That is a different project by someone else with the same name. | `.\.venv\Scripts\python.exe -m pip uninstall -y sextant`, then `.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps sextant-ai`. The uninstall also removes the `sextant` program the two projects share, and a plain install (step 3 included) answers "already satisfied" without putting it back. From version 0.1.1, `.\.venv\Scripts\sextant-doctor.exe` names this problem when it sees it; version 0.1.0 does not, so use the two lines above. |

Still stuck? Open an issue at https://github.com/protechtus/sextant/issues and paste
the line you typed and the last ten lines the screen printed. That is all we need, and
no question is too basic.

### Saving what the screen said to a file

When a command prints more than you can scroll back through, run it again with
`*> "$HOME\Desktop\sextant-log.txt"` added to the end. For example:

```powershell
.\.venv\Scripts\sextant.exe verify *> "$HOME\Desktop\sextant-log.txt"
```

Nothing appears on screen this time. Everything the command would have printed goes
into a file named `sextant-log.txt` on your Desktop, from the first line to the last.
Attach that file to your issue. Look through it first: it can contain folder names and
your Windows user name.

## Updating later

```powershell
cd $HOME\sextant-install
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps https://github.com/protechtus/sextant/archive/refs/heads/main.zip
```
