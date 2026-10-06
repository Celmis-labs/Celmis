"""What "how is X done in this repo" looks for, as data.

One table per topic. A :class:`Marker` is a regex plus a kind and a weight:

* ``import``  the file pulls in the library that does the job (+3);
* ``call``    it constructs or calls the thing (+3);
* ``config``  a configuration file that sets it up (+2);

and the topic adds path hints (a file called ``session.py`` under ``db/``),
env-name hints (which environment variables belong to the topic) and the
generic cues the engine adds on top (an env read within a few lines of a call,
+2). Nothing here knows about any one repository; precision per language is
the table's job and the tests' to measure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOPICS: tuple[str, ...] = (
    "db", "auth", "config", "http_client", "logging", "messaging", "cache",
)

# Free text -> topic. The first topic whose keyword appears wins; longer
# phrases are listed first so "http client" beats "client".
_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("http client", "http_client"), ("http_client", "http_client"), ("api client", "http_client"),
    ("requests", "http_client"), ("axios", "http_client"), ("fetch", "http_client"),
    ("httpx", "http_client"), ("rest client", "http_client"),
    ("authorization", "auth"), ("authentication", "auth"), ("authoriz", "auth"),
    ("authent", "auth"), ("auth", "auth"), ("jwt", "auth"), ("oauth", "auth"),
    ("oidc", "auth"), ("keycloak", "auth"), ("login", "auth"), ("sso", "auth"),
    ("middleware", "auth"), ("permission", "auth"),
    ("message queue", "messaging"), ("messaging", "messaging"), ("rabbit", "messaging"),
    ("kafka", "messaging"), ("amqp", "messaging"), ("queue", "messaging"),
    ("pubsub", "messaging"), ("pub/sub", "messaging"), ("celery", "messaging"),
    ("cache", "cache"), ("caching", "cache"), ("redis", "cache"), ("memcache", "cache"),
    ("logging", "logging"), ("logger", "logging"), ("logs", "logging"), ("log ", "logging"),
    ("credential", "config"), ("secret", "config"), ("settings", "config"),
    ("configuration", "config"), ("config", "config"), ("environment", "config"),
    ("env var", "config"), ("dotenv", "config"),
    ("database", "db"), ("db", "db"), ("postgres", "db"), ("mysql", "db"), ("mongo", "db"),
    ("sql", "db"), ("orm", "db"), ("connection", "db"), ("connect", "db"),
    ("sqlite", "db"), ("pool", "db"),
)


def topic_for(text: str) -> str | None:
    """Map a topic name or a few free words to one of :data:`TOPICS`.

    The keyword that appears first in the text wins ("db connection
    credentials" is about the database, "authorization and credentials" about
    auth); among keywords at the same place the longer one wins.
    """
    t = f" {(text or '').strip().lower()} "
    if t.strip() in TOPICS:
        return t.strip()
    best: tuple[int, int, str] | None = None
    for kw, topic in _KEYWORDS:
        i = t.find(kw)
        if i < 0:
            continue
        cand = (i, -len(kw), topic)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


@dataclass(frozen=True)
class Marker:
    kind: str  # "import" | "call" | "config"
    rx: re.Pattern[str]
    weight: int = 3
    label: str = ""


def _m(kind: str, pattern: str, weight: int = 3, label: str = "") -> Marker:
    return Marker(kind, re.compile(pattern, re.MULTILINE), weight, label)


@dataclass(frozen=True)
class Topic:
    name: str
    summary: str
    markers: tuple[Marker, ...]
    path_hint: re.Pattern[str]
    # Environment / setting names that belong to the topic (used to keep the
    # names list on topic when a whole Settings class lands in a slice).
    name_hint: re.Pattern[str]
    # Config file names (lower-case, fnmatch) that carry the topic's settings.
    config_files: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _p(rx: str) -> re.Pattern[str]:
    return re.compile(rx, re.IGNORECASE)


_DB = Topic(
    name="db",
    summary="database connection",
    markers=(
        # Python
        _m("import", r"^\s*(?:from|import)\s+(?:sqlalchemy|asyncpg|psycopg2?|pymongo|motor|aiomysql|pymysql|"
                     r"sqlite3|databases|tortoise|peewee|sqlmodel|django\.db|aiosqlite|oracledb|cx_Oracle)\b", label="py import"),
        _m("call", r"\b(?:create_engine|create_async_engine|asyncpg\.(?:connect|create_pool)|psycopg2?\.(?:connect|pool)|"
                   r"psycopg_pool|MongoClient|AsyncIOMotorClient|sqlite3\.connect|aiosqlite\.connect|"
                   r"pymysql\.connect|aiomysql\.(?:connect|create_pool)|sessionmaker|async_sessionmaker|"
                   r"databases\.Database)\s*\(", label="py connect"),
        _m("call", r"^\s*DATABASES\s*=", label="django DATABASES"),
        # JS / TS
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:pg|mysql2?|knex|typeorm|@prisma/client|sequelize|mongoose|mongodb|"""
                     r"""better-sqlite3|drizzle-orm[\w/-]*|@neondatabase/serverless|kysely|mssql|oracledb)['"]""", label="js import"),
        _m("call", r"\b(?:new\s+(?:Pool|Client|DataSource|Sequelize|PrismaClient|MongoClient|Kysely)\s*\(|"
                   r"knex\s*\(|mongoose\.connect\s*\(|createConnection\s*\(|createPool\s*\(|drizzle\s*\()", label="js connect"),
        _m("config", r"^\s*datasource\s+\w+\s*\{", label="prisma datasource"),
        # Go
        _m("import", r"""^\s*"(?:database/sql|github\.com/jackc/pgx[\w/]*|gorm\.io/[\w/]+|go\.mongodb\.org/[\w/-]+|"""
                     r"""github\.com/lib/pq|github\.com/jmoiron/sqlx|github\.com/go-sql-driver/mysql)"\s*$""", label="go import"),
        _m("call", r"\b(?:sql\.Open|sqlx\.(?:Connect|Open)|pgxpool\.New(?:WithConfig)?|pgx\.Connect|gorm\.Open|mongo\.Connect)\s*\(", label="go connect"),
        # Java / Kotlin
        _m("import", r"^\s*import\s+(?:javax\.sql|jakarta\.persistence|com\.zaxxer\.hikari|org\.springframework\.jdbc|"
                     r"org\.hibernate|org\.jooq|com\.mongodb)\b", label="jvm import"),
        _m("call", r"\b(?:new\s+HikariConfig|HikariDataSource\s*\(|DriverManager\.getConnection|DataSourceBuilder\.create|MongoClients\.create)", label="jvm connect"),
        _m("config", r"^\s*spring\.datasource\.\w+|^\s*spring:\s*$|^\s*datasource:\s*$", 2, label="spring datasource"),
        # PHP
        _m("call", r"\bnew\s+PDO\s*\(|\bDriverManager::getConnection|\bDB::connection\(", label="php connect"),
        _m("config", r"""['"]connections['"]\s*=>|env\(\s*['"]DB_""", 2, label="laravel database"),
        # C#
        _m("call", r"\b(?:UseNpgsql|UseSqlServer|UseMySql|UseSqlite|AddDbContext(?:Pool)?|new\s+(?:NpgsqlConnection|SqlConnection|MySqlConnection)|AddNpgsql)\b", label="cs connect"),
        _m("config", r"""['"]ConnectionStrings['"]\s*:""", 2, label="appsettings ConnectionStrings"),
        # Ruby
        _m("call", r"\bActiveRecord::Base\.establish_connection|\bSequel\.connect|PG\.connect\b", label="rb connect"),
    ),
    path_hint=_p(r"(?:^|/)(?:db|database|databases|persistence|storage|repositor(?:y|ies)|models?|infra(?:structure)?|data)(?:/|\.|_)|(?:session|connection|engine|pool|database|db)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:db|database|datasource|postgres(?:ql)?|pg|mysql|mongo(?:db)?|sql|sqlite|dsn|jdbc|connection[_-]?string)|^(?:DATABASE|DB|PG|POSTGRES|MYSQL|MONGO|SQL|JDBC|DSN)"),
    config_files=("application*.yml", "application*.yaml", "application*.properties", "appsettings*.json",
                  "database.php", "schema.prisma", "ormconfig.*", "knexfile.*", "alembic.ini", "database.yml"),
)

_AUTH = Topic(
    name="auth",
    summary="authentication and authorization",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:jwt|jose|authlib|keycloak|python_keycloak|fastapi\.security|"
                     r"flask_jwt_extended|flask_login|django\.contrib\.auth|passlib|itsdangerous|oauthlib|"
                     r"starlette\.authentication)\b", label="py import"),
        _m("call", r"\b(?:jwt\.(?:decode|encode)|jose\.jwt|KeycloakOpenID|OAuth2PasswordBearer|HTTPBearer|"
                   r"OAuth2AuthorizationCodeBearer|Depends\(\s*get_current_\w+|AuthenticationMiddleware|"
                   r"SessionMiddleware|login_required|permission_classes|JWKClient|PyJWKClient)\b", label="py auth"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:jsonwebtoken|jose|passport[\w-]*|next-auth[\w/]*|keycloak-js|"""
                     r"""keycloak-connect|express-session|@auth/[\w-]+|express-jwt|oidc-client[\w-]*|@nestjs/passport|@nestjs/jwt|auth0[\w/-]*)['"]""", label="js import"),
        _m("call", r"\b(?:jwt\.verify|jwt\.sign|passport\.(?:use|authenticate)|NextAuth\s*\(|expressjwt\s*\(|"
                   r"app\.use\(\s*(?:auth|authenticate|requireAuth|verifyToken)\w*|session\s*\(\s*\{|"
                   r"new\s+Keycloak\s*\(|getServerSession|useSession\s*\(|AuthGuard\s*\()", label="js auth"),
        _m("import", r"""^\s*"(?:github\.com/golang-jwt/jwt[\w/]*|github\.com/coreos/go-oidc[\w/]*|github\.com/auth0/[\w/-]+|golang\.org/x/oauth2[\w/]*)"\s*$""", label="go import"),
        _m("call", r"\b(?:jwt\.Parse(?:WithClaims)?|oidc\.NewProvider|oauth2\.Config\s*\{|func\s*\(next\s+http\.Handler\)\s+http\.Handler)", label="go auth"),
        _m("import", r"^\s*import\s+(?:io\.jsonwebtoken|org\.springframework\.security|org\.keycloak|com\.auth0|javax\.ws\.rs\.container\.ContainerRequestFilter)\b", label="jvm import"),
        _m("call", r"@PreAuthorize|@Secured|\boauth2ResourceServer\b|SecurityFilterChain|@RolesAllowed|JwtDecoder\b|issuer-uri", label="jvm auth"),
        _m("call", r"""['"]auth:api['"]|\bAuth::(?:user|check|guard)\b|->middleware\(\s*['"]auth|JWTAuth::|Sanctum""", label="php auth"),
        _m("call", r"\b(?:AddAuthentication|AddJwtBearer|AddOpenIdConnect|UseAuthentication|UseAuthorization|\[Authorize(?:\(|\])|AddAuthorization)", label="cs auth"),
        _m("call", r"grant_type\s*[=:]\s*['\"]?client_credentials|\.well-known/openid-configuration|jwks_uri|/protocol/openid-connect/", label="oidc flow"),
        _m("config", r"^\s*(?:issuer-uri|jwk-set-uri|jwks_uri|issuer|audience)\s*[:=]", 2, label="oidc setting"),
    ),
    path_hint=_p(r"(?:^|/)(?:auth|authn|authz|authentication|authorization|security|middleware|guards?|permissions?|login|sso|oidc|keycloak|identity|jwt)(?:/|\.|_)|(?:auth|jwt|security|middleware|permission)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:auth|jwt|oidc|oauth|keycloak|realm|issuer|audience|jwks|client[_-]?(?:id|secret)|secret|token|session|cookie|sso|scope|saml)|^(?:AUTH|JWT|OIDC|OAUTH|KEYCLOAK|SESSION|COOKIE|CLIENT_|SSO|ISSUER|AUDIENCE|JWKS|SECRET|TOKEN)"),
    config_files=("application*.yml", "application*.properties", "keycloak*.json", "realm*.json", "auth*.yml"),
)

_CONFIG = Topic(
    name="config",
    summary="configuration and credentials loading",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:pydantic_settings|dotenv|decouple|environs|dynaconf|configparser|hvac|boto3\.session)\b|"
                     r"^\s*from\s+pydantic\s+import\s+.*BaseSettings", label="py import"),
        _m("call", r"\bclass\s+\w+\(\s*(?:[\w.]*)BaseSettings\s*\)|\bload_dotenv\s*\(|\bos\.environ\b|\bos\.getenv\s*\(|\bconfig\(\s*['\"]|"
                   r"\bread_secret_version\s*\(|\bget_secret_value\s*\(|\bSettingsConfigDict\s*\(", label="py config"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:dotenv[\w/]*|@nestjs/config|config|convict|envalid|node-config|zod)['"]""", label="js import"),
        _m("call", r"\bprocess\.env\.\w+|\bprocess\.env\[|\bimport\.meta\.env\.\w+|\bConfigService\b|\bdotenv\.config\s*\(|\bconfigService\.get", label="js config"),
        _m("call", r"\b(?:viper\.(?:Get\w*|BindEnv|AutomaticEnv|SetConfigFile)|os\.(?:Getenv|LookupEnv)|envconfig\.Process|env\.Parse)\s*\(|`envconfig:\"|`env:\"", label="go config"),
        _m("call", r"@Value\(\s*\"\$\{|@ConfigurationProperties|System\.getenv\s*\(|@PropertySource", label="jvm config"),
        _m("call", r"""\benv\(\s*['"]\w+|\bgetenv\(\s*['"]|\$_ENV\[|\$_SERVER\[""", label="php config"),
        _m("call", r"\bIConfiguration\b|GetEnvironmentVariable\s*\(|\bConfiguration\[|\bGetConnectionString\s*\(|IOptions<", label="cs config"),
        _m("call", r"/run/secrets/|\bvault:\w|\bsecretKeyRef\b|\bSecretsManager\b", label="secret store"),
    ),
    path_hint=_p(r"(?:^|/)(?:config|configs|configuration|settings|conf|env)(?:/|\.|_)|(?:config|settings|env|secrets?)\w*\.\w+$"),
    name_hint=_p(r".*"),
    config_files=(".env.example", ".env.sample", "*.env.example", "config*.yml", "config*.yaml", "settings*.yml",
                  "application*.yml", "application*.properties", "appsettings*.json", "values.yaml"),
)

_HTTP = Topic(
    name="http_client",
    summary="outgoing HTTP clients",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:requests|httpx|aiohttp|urllib3|urllib\.request|httplib2|pycurl|tenacity)\b", label="py import"),
        _m("call", r"\b(?:requests\.(?:Session|get|post|put|delete|request)|httpx\.(?:Async)?Client|aiohttp\.ClientSession|"
                   r"urllib3\.PoolManager|HTTPAdapter|Retry\s*\()", label="py http"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:axios|node-fetch|got|undici|ky|superagent|@angular/common/http|cross-fetch)['"]""", label="js import"),
        _m("call", r"\b(?:axios\.(?:create|get|post)|new\s+Agent\s*\(|\bfetch\s*\(|got\.extend|ky\.create|HttpClient\s*\()", label="js http"),
        _m("call", r"\b(?:http\.Client\s*\{|http\.NewRequest(?:WithContext)?\s*\(|resty\.New\s*\(|http\.Transport\s*\{)", label="go http"),
        _m("call", r"\b(?:new\s+RestTemplate|WebClient\.(?:create|builder)|HttpClient\.new|OkHttpClient|RestClient\.create|Feign\w*)", label="jvm http"),
        _m("call", r"\bnew\s+(?:GuzzleHttp\\Client|Client)\s*\(|Http::(?:get|post|withHeaders|baseUrl)|curl_init\s*\(", label="php http"),
        _m("call", r"\b(?:AddHttpClient|IHttpClientFactory|new\s+HttpClient|HttpClient\s+\w+\s*=)", label="cs http"),
    ),
    path_hint=_p(r"(?:^|/)(?:clients?|http|api|integrations?|services?|adapters?|gateways?|connectors?)(?:/|\.|_)|(?:client|http|gateway|adapter)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:url|uri|host|base|endpoint|timeout|retries|retry|proxy|api[_-]?key|token|service)|^(?:API|BASE_URL|SERVICE_|.*_URL$|.*_HOST$|.*_TIMEOUT$)"),
)

_LOG = Topic(
    name="logging",
    summary="logging setup",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:logging(?:\.config)?|structlog|loguru|sentry_sdk|opentelemetry)\b", label="py import"),
        _m("call", r"\b(?:logging\.(?:basicConfig|config\.dictConfig|getLogger|StreamHandler|FileHandler)|structlog\.configure|logger\.add|dictConfig|getLogger)\s*\(", label="py logging"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:winston[\w-]*|pino[\w-]*|bunyan|log4js|morgan|loglevel|@sentry/[\w-]+|debug)['"]""", label="js import"),
        _m("call", r"\b(?:winston\.createLogger|pino\s*\(|bunyan\.createLogger|log4js\.getLogger|morgan\s*\(|createLogger\s*\()", label="js logging"),
        _m("call", r"\b(?:zap\.New\w*|slog\.New\w*|logrus\.New|zerolog\.New|log\.SetFlags|slog\.SetDefault)\s*\(", label="go logging"),
        _m("call", r"\b(?:LoggerFactory\.getLogger|LogManager\.getLogger|@Slf4j|logback)|<configuration|<appender\b", label="jvm logging"),
        _m("call", r"\b(?:new\s+Logger\s*\(|Log::(?:info|channel)|->pushHandler|Monolog\\)", label="php logging"),
        _m("call", r"\b(?:AddSerilog|UseSerilog|ILogger<|AddLogging|LoggerConfiguration)", label="cs logging"),
    ),
    path_hint=_p(r"(?:^|/)(?:logging|logs?|observability|telemetry)(?:/|\.|_)|(?:log|logger|logging)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:log|logging|loglevel|level|sentry|dsn|loki|syslog|format)|^(?:LOG|SENTRY|LOKI)"),
    config_files=("logback*.xml", "log4j*.xml", "log4j*.properties", "logging*.yml", "logging*.yaml", "logging*.json"),
)

_MSG = Topic(
    name="messaging",
    summary="message queue / broker clients",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:pika|aio_pika|kafka|aiokafka|confluent_kafka|celery|kombu|nats|redis\.asyncio|faststream|google\.cloud\.pubsub)\b", label="py import"),
        _m("call", r"\b(?:pika\.(?:BlockingConnection|URLParameters|ConnectionParameters)|aio_pika\.connect(?:_robust)?|KafkaProducer|KafkaConsumer|AIOKafka(?:Producer|Consumer)|Celery\s*\(|Consumer\s*\(\s*\{|nats\.connect)", label="py broker"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:amqplib|amqp-connection-manager|kafkajs|bullmq?|nats|@nestjs/microservices|@google-cloud/pubsub|ioredis)['"]""", label="js import"),
        _m("call", r"\b(?:amqp\.connect|new\s+Kafka\s*\(|new\s+Queue\s*\(|new\s+Worker\s*\(|ClientsModule\.register)", label="js broker"),
        _m("call", r"\b(?:amqp\.Dial|sarama\.New\w+|kafka\.NewReader|kafka\.NewWriter|nats\.Connect)\s*\(", label="go broker"),
        _m("call", r"\b(?:@RabbitListener|@KafkaListener|RabbitTemplate|KafkaTemplate|ConnectionFactory\s*\(|JmsTemplate)", label="jvm broker"),
        _m("call", r"\b(?:AMQPStreamConnection|RdKafka\\|Queue::push|dispatch\s*\()", label="php broker"),
        _m("call", r"\b(?:AddMassTransit|UsingRabbitMq|ServiceBusClient|IConnectionFactory|ConnectionFactory\s*\{)", label="cs broker"),
    ),
    path_hint=_p(r"(?:^|/)(?:queues?|messaging|messages?|events?|consumers?|producers?|workers?|brokers?|tasks?|jobs?)(?:/|\.|_)|(?:queue|broker|consumer|producer|worker)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:amqp|rabbit|kafka|broker|queue|nats|topic|exchange|vhost|bootstrap)|^(?:AMQP|RABBIT|KAFKA|BROKER|QUEUE|NATS|CELERY)"),
)

_CACHE = Topic(
    name="cache",
    summary="cache clients",
    markers=(
        _m("import", r"^\s*(?:from|import)\s+(?:redis(?:\.asyncio)?|aioredis|pymemcache|cachetools|diskcache|django\.core\.cache|fastapi_cache|aiocache|dogpile)\b", label="py import"),
        _m("call", r"\b(?:redis\.(?:Redis|from_url|ConnectionPool|StrictRedis)|aioredis\.\w+|Redis\.from_url|pymemcache\.\w+|TTLCache|lru_cache|FastAPICache\.init|caches\[)", label="py cache"),
        _m("import", r"""(?:from\s+|require\(\s*)['"](?:ioredis|redis|node-cache|lru-cache|memcached|cache-manager[\w-]*|@upstash/redis)['"]""", label="js import"),
        _m("call", r"\b(?:new\s+(?:Redis|NodeCache|LRUCache)\s*\(|createClient\s*\(|CacheModule\.register)", label="js cache"),
        _m("call", r"\b(?:redis\.NewClient|redis\.NewUniversalClient|redis\.ParseURL|bigcache\.New|ristretto\.NewCache|memcache\.New)\s*\(", label="go cache"),
        _m("call", r"\b(?:RedisTemplate|@Cacheable|CacheManager|Caffeine\.newBuilder|JedisPool|LettuceConnectionFactory)", label="jvm cache"),
        _m("call", r"\b(?:Cache::(?:remember|get|put)|Redis::connection|new\s+Memcached)", label="php cache"),
        _m("call", r"\b(?:AddStackExchangeRedisCache|AddMemoryCache|ConnectionMultiplexer\.Connect|IDistributedCache|IMemoryCache)", label="cs cache"),
    ),
    path_hint=_p(r"(?:^|/)(?:cache|caching|redis)(?:/|\.|_)|(?:cache|redis)\w*\.\w+$"),
    name_hint=_p(r"(?:^|[._-])(?:redis|cache|memcache|ttl)|^(?:REDIS|CACHE|MEMCACHE)"),
)

DETECTORS: dict[str, Topic] = {
    t.name: t for t in (_DB, _AUTH, _CONFIG, _HTTP, _LOG, _MSG, _CACHE)
}

# Source files the scanner reads (by extension) and the config files it also
# considers, with the per-file size limit.
SOURCE_EXT = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java", ".kt", ".kts",
    ".php", ".cs", ".rb", ".prisma", ".vue", ".scala", ".rs",
})
CONFIG_EXT = frozenset({".yml", ".yaml", ".properties", ".json", ".toml", ".ini", ".xml", ".conf"})
MAX_FILE_BYTES = 200_000
MAX_FILES_SCANNED = 6000

# Demoted: these show how it is *tested*, not how it is done.
DEMOTE_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs|mocks?|__mocks__|fixtures?|e2e|migrations?|alembic|examples?|docs?|"
    r"node_modules|vendor|dist|build|\.venv|venv|site-packages)(?:/|$)|"
    r"(?:^|/)(?:test_[^/]*|[^/]*_test\.\w+|[^/]*\.(?:test|spec)\.\w+|conftest\.py)$",
    re.IGNORECASE,
)

__all__ = [
    "CONFIG_EXT", "DEMOTE_PATH", "DETECTORS", "MAX_FILES_SCANNED", "MAX_FILE_BYTES",
    "Marker", "SOURCE_EXT", "TOPICS", "Topic", "topic_for",
]
