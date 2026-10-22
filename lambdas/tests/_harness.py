"""Shared test plumbing. stdlib only: unittest.mock stands in for boto3 and urllib.

Each Lambda directory has its own `handler.py`, so the modules are loaded by
path under distinct names. Environment variables are set before import because
the handlers read them at module level. AWS credentials are pointed at dummy
values and /dev/null config files so botocore can never pick up a real session.
"""

import importlib.util
import io
import json
import os
import pathlib
import urllib.error
import urllib.request
from unittest import mock

from botocore.exceptions import ClientError

LAMBDAS = pathlib.Path(__file__).resolve().parent.parent

ENV = {
    "AWS_DEFAULT_REGION": "ap-southeast-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_CONFIG_FILE": "/dev/null",
    "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    "DRAFTS_TABLE": "drafts-test",
    "SHAPE_QUEUE_URL": "https://sqs.test/shape",
    "WEBHOOK_SECRET": "hook-secret",
    "ALLOWED_USER_ID": "42",
    "DRAFT_TTL_HOURS": "24",
    "BRAINDUMP_REPO": "braindump",
    "GITHUB_OWNER": "owner",
    "ANTHROPIC_MODEL": "test-model",
    "TELEGRAM_BOT_TOKEN_PARAM": "/anyissues-test/telegram-bot-token",
    "ANTHROPIC_API_KEY_PARAM": "/anyissues-test/anthropic-api-key",
    "GITHUB_PAT_PARAM": "/anyissues-test/github-pat",
    "DIGEST_FUNCTION_NAME": "anyissue-digest",
    # Left unset by default: _wif_configured() is False, so every test exercises
    # the SSM-key fallback path exactly as before. WIF-specific tests set the
    # three module-level constants directly (see wif_enabled() below) rather
    # than relying on env vars, since those are bound once at import time.
}

_modules = {}


def load_handler(name):
    """Import lambdas/<name>/handler.py once, with every AWS client mocked."""
    if name in _modules:
        return _modules[name]
    os.environ.pop("AWS_PROFILE", None)
    os.environ.update(ENV)
    spec = importlib.util.spec_from_file_location(f"{name}_handler", LAMBDAS / name / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _modules[name] = module
    return module


def reset_module(module):
    """Fresh mocks for every AWS client the handler holds at module level."""
    for attr in ("drafts_table", "sqs", "ssm", "sts", "lambda_client"):
        if hasattr(module, attr):
            setattr(module, attr, mock.MagicMock())
    module.ssm.get_parameter.return_value = {"Parameter": {"Value": "s3cret"}}
    module._cache.clear()
    if hasattr(module, "sts"):
        module.sts.get_web_identity_token.return_value = {"WebIdentityToken": "eyJ.fake-jwt.sig"}
    if hasattr(module, "_wif"):
        module._wif.clear()


class wif_enabled:
    """Context manager: point a handler module's WIF constants at test values
    for the duration of the `with` block, then restore them. These are bound
    once at import time from os.environ, so patching os.environ after import
    has no effect -- the module attributes themselves must be set directly."""

    ATTRS = (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
    )

    def __init__(self, module, rule_id="fdrl_test", org_id="org_test", service_account_id="svac_test"):
        self.module = module
        self.values = (rule_id, org_id, service_account_id)

    def __enter__(self):
        self.originals = {attr: getattr(self.module, attr) for attr in self.ATTRS}
        for attr, value in zip(self.ATTRS, self.values, strict=True):
            setattr(self.module, attr, value)
        return self

    def __exit__(self, *exc):
        for attr, value in self.originals.items():
            setattr(self.module, attr, value)


def conditional_check_failed(op="PutItem"):
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, op)


class _Response(io.BytesIO):
    headers = {}  # urllib responses expose HTTP headers here; nothing needs any by default

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeHTTP:
    """Stand-in for urllib.request.urlopen that records every outbound call.

    Responses are chosen by host; override per test through the `telegram`,
    `github`, `anthropic` callables (each gets the parsed request and returns
    a JSON-able body) or raise via `fail[host] = status`. The Anthropic OAuth
    token exchange (`/v1/oauth/token`, same host as `/v1/messages`) is routed
    separately through `oauth` / `oauth_fail`, so a WIF test can fail the
    exchange without also failing the SSM-key fallback's Messages call.
    """

    def __init__(self):
        self.calls = []  # (host, path, method, body, headers)
        self.fail = {}  # host -> HTTP status to raise
        self.oauth_fail = None  # HTTP status to raise for the oauth exchange specifically
        self.telegram = lambda path, body: {"ok": True, "result": {"message_id": 777}}
        self.github = lambda path, body: {
            "items": [{"name": "repo-a"}, {"name": "repo-b"}],
            "html_url": "https://github.com/owner/repo-a/issues/1",
        }
        self.anthropic = lambda path, body: llm_response('{"title": "Do the thing", "body": "- AC one"}')
        self.oauth = lambda path, body: {"access_token": "sk-ant-oat01-test", "expires_in": 600, "token_type": "bearer"}

    def __call__(self, req, timeout=None):
        url = req.full_url
        host = "telegram" if "api.telegram.org" in url else "github" if "api.github.com" in url else "anthropic"
        path = url.split(host_domain(host), 1)[1]
        body = json.loads(req.data) if req.data else None
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((host, path, req.get_method(), body, headers))
        if host == "anthropic" and path == "/v1/oauth/token":
            if self.oauth_fail:
                raise urllib.error.HTTPError(
                    url, self.oauth_fail, "err", {}, io.BytesIO(b'{"error":"simulated_oauth_failure"}')
                )
            return _Response(json.dumps(self.oauth(path, body)).encode())
        if host in self.fail:
            status = self.fail[host]
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(b'{"description":"simulated"}'))
        payload = getattr(self, host)(path, body)
        return _Response(json.dumps(payload).encode())

    def sent(self, host, path_suffix=None):
        return [
            body
            for h, p, _m, body, _hdrs in self.calls
            if h == host and (path_suffix is None or p.endswith(path_suffix))
        ]

    def headers_sent(self, host, path_suffix=None):
        return [
            hdrs for h, p, _m, _b, hdrs in self.calls if h == host and (path_suffix is None or p.endswith(path_suffix))
        ]

    def patch(self):
        return mock.patch.object(urllib.request, "urlopen", self)


def host_domain(host):
    return {"telegram": "api.telegram.org", "github": "api.github.com", "anthropic": "api.anthropic.com"}[host]


def llm_response(text, stop_reason="end_turn"):
    return {"stop_reason": stop_reason, "content": [{"type": "text", "text": text}]}


def webhook_event(update, secret=ENV["WEBHOOK_SECRET"]):
    return {"headers": {"x-telegram-bot-api-secret-token": secret}, "body": json.dumps(update)}


def message_update(update_id, text, user_id=42, chat_id=42):
    return {"update_id": update_id, "message": {"from": {"id": user_id}, "chat": {"id": chat_id}, "text": text}}


def callback_update(update_id, data, user_id=42):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "from": {"id": user_id}, "data": data}}


# The full DynamoDB reserved-words list (case-insensitive), from
# https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ReservedWords.html
# We got bitten once by hand-picking a subset (ttl/type/status) and missing
# "project" -- this checks every bare identifier in an UpdateExpression
# against the complete list instead of a guess at which ones matter.
DYNAMODB_RESERVED_WORDS = frozenset(
    """ABORT ABSOLUTE ACTION ADD AFTER AGENT AGGREGATE ALL ALLOCATE ALTER ANALYZE AND ANY ARCHIVE ARE ARRAY AS ASC
    ASCII ASENSITIVE ASSERTION ASYMMETRIC AT ATOMIC ATTACH ATTRIBUTE AUTH AUTHORIZATION AUTHORIZE AUTO AVG BACK
    BACKUP BASE BATCH BEFORE BEGIN BETWEEN BIGINT BINARY BIT BLOB BLOCK BOOLEAN BOTH BREADTH BUCKET BULK BY BYTE
    CALL CALLED CALLING CAPACITY CASCADE CASCADED CASE CAST CATALOG CHAR CHARACTER CHECK CLASS CLOB CLOSE CLUSTER
    CLUSTERED CLUSTERING CLUSTERS COALESCE COLLATE COLLATION COLLECTION COLUMN COLUMNS COMBINE COMMENT COMMIT
    COMPACT COMPILE COMPRESS CONDITION CONFLICT CONNECT CONNECTION CONSISTENCY CONSISTENT CONSTRAINT CONSTRAINTS
    CONSTRUCTOR CONSUMED CONTINUE CONVERT COPY CORRESPONDING COUNT COUNTER CREATE CROSS CUBE CURRENT CURSOR CYCLE
    DATA DATABASE DATE DATETIME DAY DEALLOCATE DEC DECIMAL DECLARE DEFAULT DEFERRABLE DEFERRED DEFINE DEFINED
    DEFINITION DELETE DELIMITED DEPTH DEREF DESC DESCRIBE DESCRIPTOR DETACH DETERMINISTIC DIAGNOSTICS DIRECTORIES
    DISABLE DISCONNECT DISTINCT DISTRIBUTE DO DOMAIN DOUBLE DROP DUMP DURATION DYNAMIC EACH ELEMENT ELSE ELSEIF
    EMPTY ENABLE END EQUAL EQUALS ERROR ESCAPE ESCAPED EVAL EVALUATE EXCEEDED EXCEPT EXCEPTION EXCEPTIONS EXCLUSIVE
    EXEC EXECUTE EXISTS EXIT EXPLAIN EXPLODE EXPORT EXPRESSION EXTENDED EXTERNAL EXTRACT FAIL FALSE FAMILY FETCH
    FIELDS FILE FILTER FILTERING FINAL FINISH FIRST FIXED FLATTERN FLOAT FOR FORCE FOREIGN FORMAT FORWARD FOUND
    FREE FROM FULL FUNCTION FUNCTIONS GENERAL GENERATE GET GLOB GLOBAL GO GOTO GRANT GREATER GROUP GROUPING
    HANDLER HASH HAVE HAVING HEAP HIDDEN HOLD HOUR IDENTIFIED IDENTITY IF IGNORE IMMEDIATE IMPORT IN INCLUDING
    INCLUSIVE INCREMENT INCREMENTAL INDEX INDEXED INDEXES INDICATOR INFINITE INITIALLY INLINE INNER INNTER INOUT
    INPUT INSENSITIVE INSERT INSTEAD INT INTEGER INTERSECT INTERVAL INTO INVALIDATE IS ISOLATION ITEM ITEMS
    ITERATE JOIN KEY KEYS LAG LANGUAGE LARGE LAST LATERAL LEAD LEADING LEAVE LEFT LENGTH LESS LEVEL LIKE LIMIT
    LIMITED LINES LIST LOAD LOCAL LOCALTIME LOCALTIMESTAMP LOCATION LOCATOR LOCK LOCKS LOG LOGED LONG LOOP LOWER
    MAP MATCH MATERIALIZED MAX MAXLEN MEMBER MERGE METHOD METRICS MIN MINUS MINUTE MISSING MOD MODE MODIFIES
    MODIFY MODULE MONTH MULTI MULTISET NAME NAMES NATIONAL NATURAL NCHAR NCLOB NEW NEXT NO NONE NOT NULL NULLIF
    NUMBER NUMERIC OBJECT OF OFFLINE OFFSET OLD ON ONLINE ONLY OPAQUE OPEN OPERATOR OPTION OR ORDER ORDINALITY
    OTHER OTHERS OUT OUTER OUTPUT OVER OVERLAPS OVERRIDE OWNER PAD PARALLEL PARAMETER PARAMETERS PARTIAL PARTITION
    PARTITIONED PARTITIONS PATH PERCENT PERCENTILE PERMISSION PERMISSIONS PIPE PIPELINED PLAN POOL POSITION
    PRECISION PREPARE PRESERVE PRIMARY PRIOR PRIVATE PRIVILEGES PROCEDURE PROCESSED PROJECT PROJECTION PROPERTY
    PROVISIONING PUBLIC PUT QUERY QUIT QUORUM RAISE RANDOM RANGE RANK RAW READ READS REAL REBUILD RECORD RECURSIVE
    REDUCE REF REFERENCE REFERENCES REFERENCING REGEXP REGION REINDEX RELATIVE RELEASE REMAINDER RENAME REPEAT
    REPLACE REQUEST RESET RESIGNAL RESOURCE RESPONSE RESTORE RESTRICT RESULT RETURN RETURNING RETURNS REVERSE
    REVOKE RIGHT ROLE ROLES ROLLBACK ROLLUP ROUTINE ROW ROWS RULE RULES SAMPLE SATISFIES SAVE SAVEPOINT SCAN
    SCHEMA SCOPE SCROLL SEARCH SECOND SECTION SEGMENT SEGMENTS SELECT SELF SEMI SENSITIVE SEPARATE SEQUENCE
    SERIALIZABLE SESSION SET SETS SHARD SHARE SHARED SHORT SHOW SIGNAL SIMILAR SIZE SKEWED SMALLINT SNAPSHOT SOME
    SOURCE SPACE SPACES SPARSE SPECIFIC SPECIFICTYPE SPLIT SQL SQLCODE SQLERROR SQLEXCEPTION SQLSTATE SQLWARNING
    START STATE STATIC STATUS STORAGE STORE STORED STREAM STRING STRUCT STYLE SUB SUBMULTISET SUBPARTITION
    SUBSTRING SUBTYPE SUM SUPER SYMMETRIC SYNONYM SYSTEM TABLE TABLESAMPLE TEMP TEMPORARY TERMINATED TEXT THAN
    THEN THROUGHPUT TIME TIMESTAMP TIMEZONE TINYINT TO TOKEN TOTAL TOUCH TRAILING TRANSACTION TRANSFORM TRANSLATE
    TRANSLATION TREAT TRIGGER TRIM TRUE TRUNCATE TTL TUPLE TYPE UNDER UNDO UNION UNIQUE UNIT UNKNOWN UNLOGGED
    UNNEST UNPROCESSED UNSIGNED UNTIL UPDATE UPPER URL USAGE USE USER USERS USING UUID VACUUM VALUE VALUED VALUES
    VARCHAR VARIABLE VARIANCE VARINT VARYING VIEW VIEWS VIRTUAL VOID WAIT WHEN WHENEVER WHERE WHILE WINDOW WITH
    WITHIN WITHOUT WORK WRAPPED WRITE YEAR ZONE""".split()
)


# UpdateExpression's own clause keywords: always legitimately bare -- they
# mark a clause, they aren't attribute-name references, even though they
# also happen to appear in the reserved-words list.
_UPDATE_EXPRESSION_CLAUSES = frozenset({"SET", "REMOVE", "ADD", "DELETE"})


def assert_no_bare_reserved_words(testcase, update_expression):
    """Fail if `update_expression` uses a DynamoDB reserved word as a bare
    (non-#-aliased) attribute name. Tokenizes on word boundaries and skips
    anything already prefixed with # (alias) or : (value placeholder), so it
    catches exactly the class of bug a hand-picked ttl/type/status regex
    missed: an unaliased "project" sitting right next to aliased neighbours."""
    import re

    for token in re.findall(r"(?<![#:\w])[A-Za-z_][A-Za-z0-9_]*", update_expression):
        upper = token.upper()
        if upper in DYNAMODB_RESERVED_WORDS and upper not in _UPDATE_EXPRESSION_CLAUSES:
            testcase.fail(
                f"bare reserved word {token!r} in UpdateExpression (alias it with #): {update_expression!r}"
            )
