"""Development-only protocol constants.

This module is the *only* place in the Python tree that holds the recovered
plaintext protocol constants.  ``packaging/build.ps1`` excludes it with
``--nofollow-import-to=replica.domain._dev_secrets`` (and ``build.spec`` lists
it in ``excludes``), so it must never be imported from a frozen build.

A source checkout without a built native core imports this module lazily from
the protocol functions to keep ``pytest`` runnable.  Frozen builds take the
native path instead and raise ``NativeCryptoRequired`` when the sidecar is
missing.

Do not import this module at module import time: a frozen build must fail at
the crypto call, not while the application is starting.
"""

# ---- protocol constants (recovered from binary evidence) ------------------
RUNSCHOOL_SECRET = '2slhe02lsfiwowlcixisla_sls-_slaor'
TOKEN_SIGN_SALT = '&wh2016_swcampus'
RUNEC_KEY = 'd8dba5641eace5ea41cc502ae30d3bb2'
APP_ID = 'prj7sr2xk2bntjccmzbjckps7m9ogmlz'
HOST = 'run.gxapp.iydsj.com'
UPLOAD_URL = f'https://{HOST}/api/v70260/runnings/save/record'

#: Submit-signature salt stand-in (native SportJniUtils.getSecretKey(5)).
SIGN_SALT_STANDIN = RUNSCHOOL_SECRET

#: Values the native core returns from its ``protocol_constants`` opcode.
PROTOCOL_CONSTANTS = {
    'host': HOST,
    'app_id': APP_ID,
    'runec_key': RUNEC_KEY,
}
