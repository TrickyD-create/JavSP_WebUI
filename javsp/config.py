from argparse import ArgumentParser, RawTextHelpFormatter
from enum import Enum
import os
import shutil
from typing import Dict, List, Literal, TypeAlias, Union
from confz import BaseConfig, CLArgSource, EnvSource, FileSource
from pydantic import ByteSize, Field, NonNegativeInt, PositiveInt
from pydantic_extra_types.pendulum_dt import Duration
from pydantic_core import Url
from pathlib import Path

from javsp.lib import resource_path

class Scanner(BaseConfig):
    ignored_id_pattern: List[str]
    input_directory: Path | None = None
    filename_extensions: List[str]
    ignored_folder_name_pattern: List[str]
    minimum_size: ByteSize
    skip_nfo_dir: bool
    manual: bool
    restrict_to_files: list[str] | None = None

class CrawlerID(str, Enum):
    airav = 'airav'
    avsox = 'avsox'
    javrate = 'javrate'
    avwiki = 'avwiki'
    fanza = 'fanza'
    fc2 = 'fc2'
    fc2fan = 'fc2fan'
    fc2cmadb = 'fc2cmadb'
    fc2ppvdb = 'fc2ppvdb'
    jav321 = 'jav321'
    javbus = 'javbus'
    javdb = 'javdb'
    javlib = 'javlib'
    javmenu = 'javmenu'
    mgstage = 'mgstage'
    njav = 'njav'
    prestige = 'prestige'
    arzon = 'arzon'
    arzon_iv = 'arzon_iv'

class Network(BaseConfig):
    proxy_server: Url | None
    retry: NonNegativeInt = 3
    timeout: Duration
    proxy_free: Dict[str, Url] = Field(default_factory=dict)  # 保留兼容旧配置，不再使用
    javdb_cookie: str | None = None

class CrawlerSelect(BaseConfig):
    def items(self) -> List[tuple[str, list[str]]]:
        return [
            ('normal', self.normal),
            ('fc2', self.fc2),
            ('cid', self.cid),
        ]

    def __getitem__(self, index) -> list[str]:
        match index:
            case 'normal':
                return self.normal
            case 'fc2':
                return self.fc2
            case 'cid':
                return self.cid
        raise Exception("Unknown crawler type")

    normal: list[str] = Field(default_factory=list)
    fc2: list[str] = Field(default_factory=list)
    cid: list[str] = Field(default_factory=list)

class CrawlerFieldPriorities(BaseConfig):
    title: list[str] = Field(default_factory=list)
    plot: list[str] = Field(default_factory=list)
    actress: list[str] = Field(default_factory=list)
    preview_pics: list[str] = Field(default_factory=list)

class MovieInfoField(str, Enum):
    dvdid = 'dvdid'
    cid = 'cid'
    url = 'url'
    plot = 'plot'
    cover = 'cover'
    big_cover = 'big_cover'
    genre = 'genre'
    genre_id = 'genre_id'
    genre_norm = 'genre_norm'
    score = 'score'
    title = 'title'
    ori_title = 'ori_title'
    magnet = 'magnet'
    serial = 'serial'
    actress = 'actress'
    actress_pics = 'actress_pics'
    director = 'director'
    duration = 'duration'
    producer = 'producer'
    publisher = 'publisher'
    uncensored = 'uncensored'
    publish_date = 'publish_date'
    preview_pics = 'preview_pics'
    preview_video = 'preview_video'

class UseJavDBCover(str, Enum):
    yes = "yes"
    no = "no"
    fallback = "fallback"

class Crawler(BaseConfig):
    selection: CrawlerSelect
    field_priorities: CrawlerFieldPriorities = Field(default_factory=CrawlerFieldPriorities)
    required_keys: list[MovieInfoField]
    hardworking: bool
    respect_site_avid: bool
    fc2fan_local_path: Path | None
    sleep_after_scraping: Duration
    use_javdb_cover: UseJavDBCover
    normalize_actress_name: bool

class MetadataCompleteField(BaseConfig):
    required: bool = False
    prefer_language: Literal['zh'] | None = None
    min_cjk_ratio: float = 0
    min_length: NonNegativeInt = 0
    min_items: NonNegativeInt = 0
    reject_values: list[str] = Field(default_factory=list)
    allow_overwrite: bool = False

class MetadataComplete(BaseConfig):
    enabled: bool = True
    auto_refresh: bool = True
    refresh_after: Duration = Duration(days=7)
    fields: dict[str, MetadataCompleteField] = Field(default_factory=dict)

class MovieDefault(BaseConfig):
    title: str
    actress: str
    series: str
    director: str
    producer: str
    publisher: str

class PathSummarize(BaseConfig):
    output_folder_pattern: str
    basename_pattern: str
    length_maximum: PositiveInt
    length_by_byte: bool
    max_actress_count: PositiveInt = 10
    hard_link: bool

class TitleSummarize(BaseConfig):
    remove_trailing_actor_name: bool

class NFOSummarize(BaseConfig):
    basename_pattern: str
    title_pattern: str
    custom_genres_fields: list[str]
    custom_tags_fields: list[str]
    include_actor_tmdbid: bool = False

class ExtraFanartSummarize(BaseConfig):
    enabled: bool
    scrap_interval: Duration

class CoverSummarize(BaseConfig):
    basename_pattern: str
    highres: bool
    add_label: bool

class FanartSummarize(BaseConfig):
    basename_pattern: str

class Summarizer(BaseConfig):
    default: MovieDefault
    censor_options_representation: list[str]
    title: TitleSummarize
    move_files: bool = True
    path: PathSummarize
    nfo: NFOSummarize
    cover: CoverSummarize
    fanart: FanartSummarize
    extra_fanarts: ExtraFanartSummarize

class BaiduTranslateEngine(BaseConfig):
    name: Literal['baidu']
    app_id: str
    api_key: str

class BingTranslateEngine(BaseConfig):
    name: Literal['bing']
    api_key: str

class ClaudeTranslateEngine(BaseConfig):
    name: Literal['claude']
    api_key: str

class OpenAITranslateEngine(BaseConfig):
    name: Literal['openai']
    url: Url
    api_key: str
    model: str
    system_prompt: str = "Translate the following Japanese paragraph into zh_CN, while leaving non-Japanese text, names, or text that does not look like Japanese untranslated. Reply with the translated text only, do not add any text that is not in the original content."

class GoogleTranslateEngine(BaseConfig):
    name: Literal['google']

TranslateEngine: TypeAlias = Union[
        BaiduTranslateEngine,
        BingTranslateEngine,
        ClaudeTranslateEngine,
        OpenAITranslateEngine,
        GoogleTranslateEngine,
        None]

class TranslateField(BaseConfig):
    title: bool
    plot: bool

class Translator(BaseConfig):
    engine: TranslateEngine = Field(..., discriminator='name')
    fields: TranslateField

class Other(BaseConfig):
    interactive: bool

class Daemon(BaseConfig):
    enabled: bool = False
    state_db: Path = Path('data/javsp_state.db')
    scan_interval: Duration = Duration(minutes=30)
    worker_interval: Duration = Duration(seconds=30)
    max_movies_per_run: NonNegativeInt = 0
    download_stable_seconds: NonNegativeInt = 300

class RetryPolicy(BaseConfig):
    network_retry_after: Duration = Duration(hours=1)
    metadata_retry_after: Duration = Duration(days=1)
    max_retry_count: NonNegativeInt = 3
    crawler_circuit_break_threshold: PositiveInt = 5
    crawler_circuit_break_duration: Duration = Duration(hours=6)

class Notifications(BaseConfig):
    enabled: bool = False
    webhook_url: Url | None = None
    notify_on_success: bool = True
    notify_on_failure: bool = True

def get_config_source():
    parser = ArgumentParser(prog='JavSP', description='汇总多站点数据的AV元数据刮削器', formatter_class=RawTextHelpFormatter)
    parser.add_argument('-c', '--config', help='使用指定的配置文件')
    args, _ = parser.parse_known_args()
    sources = []
    if args.config is None:
        args.config = resource_path('config.yml')
    if not os.path.exists(args.config):
        example = os.path.join(os.path.dirname(args.config), 'config.example.yml')
        if os.path.exists(example):
            shutil.copy(example, args.config)
            print(f"已从 {example} 生成默认配置 {args.config}，请按需修改")
    sources.append(FileSource(file=args.config))
    sources.append(EnvSource(prefix='JAVSP_', allow_all=True))
    sources.append(CLArgSource(prefix='o'))
    return sources

class Cfg(BaseConfig):
    scanner: Scanner
    network: Network
    crawler: Crawler
    summarizer: Summarizer
    translator: Translator
    other: Other
    daemon: Daemon = Field(default_factory=Daemon)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    notifications: Notifications = Field(default_factory=Notifications)
    metadata_complete: MetadataComplete = Field(default_factory=MetadataComplete)
    CONFIG_SOURCES=get_config_source()
