"""
pew_ptz.exitcodes — process exit codes shared by the server and its supervisor.

The supervisor restarts the server after any exit except these. The values
avoid codes that Python and the C runtime produce on their own: 1 (uncaught
exception, or a process killed by Popen.terminate), 2 (usage errors), 3
(abort() once faulthandler is enabled) and 0xC0000000 and up (native crashes),
so a crash can never be mistaken for one of them.
"""

EXIT_OK = 0
EXIT_CONFIG = 78        # bad setting or command-line argument (sysexits EX_CONFIG)
EXIT_PORT_IN_USE = 98   # the port is held by another listener, usually another pew-ptz
