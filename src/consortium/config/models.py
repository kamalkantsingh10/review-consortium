"""Typed, versioned configuration models for every Study-folder YAML file.

Every model forbids unknown fields. Files are loaded only through
``consortium.config.load``; later stories extend these models additively.
"""

from __future__ import annotations

import re
import string
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from consortium.core.personas import QUOTA_ATTRIBUTES, TRAITS

SCHEMA_VERSION = 1

_DECIMAL_PATTERN = r"^[0-9]+(\.[0-9]+)?$"
_MODEL_ID_PATTERN = r"^m[1-9][0-9]*$"
_NAME_PATTERN = r"^[a-z0-9][a-z0-9_]*$"
_TEST_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
_CLIP_ID_PATTERN = r"^c_[a-z2-7]{8}$"


def _require_decimal_string(value: Any) -> Any:
    if not isinstance(value, str) or not re.fullmatch(_DECIMAL_PATTERN, value):
        raise ValueError('must be a non-negative decimal string, for example "0.30"')
    return value


DecimalStr = Annotated[
    Decimal,
    BeforeValidator(_require_decimal_string),
    WithJsonSchema({"type": "string", "pattern": _DECIMAL_PATTERN}),
]

_POSITIVE_DECIMAL_PATTERN = r"^(?=.*[1-9])[0-9]+(\.[0-9]+)?$"


def _require_positive_decimal_string(value: Any) -> Any:
    if not isinstance(value, str) or not re.fullmatch(_POSITIVE_DECIMAL_PATTERN, value):
        raise ValueError('must be a decimal string greater than 0, for example "4"')
    return value


PositiveDecimalStr = Annotated[
    Decimal,
    BeforeValidator(_require_positive_decimal_string),
    WithJsonSchema({"type": "string", "pattern": _POSITIVE_DECIMAL_PATTERN}),
]

SchemaVersion = Annotated[Literal[1], Field(description="Config file schema version.")]


def _unique(values: list[Any], what: str) -> list[Any]:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what} {value!r}")
        seen.add(value)
    return values


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# --------------------------------------------------------------------------- study.yaml


_ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

# Settings only one provider understands (story 2.2); any other provider: config_invalid.
_PROVIDER_ONLY_SETTINGS = {
    "fps": "gemini",
    "media_resolution": "gemini",
    "thinking_level": "gemini",
}
_DEFAULT_KEY_ENV = {"gemini": "GEMINI_API_KEY", "qwen": "DASHSCOPE_API_KEY"}


class ModelSettings(_Strict):
    temperature: Annotated[StrictFloat, Field(gt=0)] = 0.7
    fps: Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)] | None = Field(
        default=None, description="Frames per second sampled from each Clip; gemini only."
    )
    seed_supported: StrictBool = Field(
        default=True, description="Send the attempt seed with each request."
    )
    media_resolution: Literal["low", "medium", "high"] | None = Field(
        default=None, description="Media resolution per frame; gemini only."
    )
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = Field(
        default=None, description="Pinned thinking level; gemini only."
    )
    api_key_env: Annotated[StrictStr, Field(pattern=_ENV_NAME_PATTERN)] | None = Field(
        default=None,
        description="Env var holding the API key (default GEMINI_API_KEY for gemini, "
        "DASHSCOPE_API_KEY for qwen); never put the key itself in a Study file.",
    )


class MediaLimits(_Strict):
    """What a Model accepts per request; ``push test`` checks every Trial against it."""

    max_seconds: Annotated[StrictFloat, Field(gt=0)]
    max_bytes: Annotated[StrictInt, Field(gt=0)]
    inline_base64: StrictBool = True


class FakeSettings(_Strict):
    """What the Fake rater reports as usage for each answer (story 1.9), how often it
    answers invalidly (story 1.10) and how often it simulates a transient error, a
    refusal or a fatal error (story 2.1)."""

    input_tokens: Annotated[StrictInt, Field(ge=0)] = 0
    output_tokens: Annotated[StrictInt, Field(ge=0)] = 0
    invalid_rate: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default=0.0,
        description="Probability (0-1) that an attempt's answer is invalid; decided per "
        "attempt seed, so deterministic.",
    )
    transient_rate: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default=0.0,
        description="Probability (0-1) that an attempt simulates a rate limit or transport "
        "error (category transient); decided per attempt seed.",
    )
    refusal_rate: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default=0.0,
        description="Probability (0-1) that an attempt simulates a safety refusal "
        "(category refused); decided per attempt seed.",
    )
    fatal_rate: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default=0.0,
        description="Probability (0-1) that an attempt simulates a fatal provider error "
        "(category fatal); decided per attempt seed.",
    )


class ModelConfig(_Strict):
    id: Annotated[StrictStr, Field(pattern=_MODEL_ID_PATTERN)]
    provider: Literal["fake", "gemini", "qwen"]
    model: StrictStr
    settings: ModelSettings = Field(default_factory=ModelSettings)
    max_output_tokens: Annotated[StrictInt, Field(gt=0)]
    limits: MediaLimits
    fake: FakeSettings | None = Field(
        default=None, description="Fake rater settings; only for provider: fake."
    )

    @model_validator(mode="after")
    def _fake_only_for_fake(self) -> ModelConfig:
        if self.fake is not None and self.provider != "fake":
            raise ValueError("fake: only allowed for provider: fake")
        return self

    @model_validator(mode="after")
    def _settings_fit_provider(self) -> ModelConfig:
        for name, provider in _PROVIDER_ONLY_SETTINGS.items():
            if getattr(self.settings, name) is not None and self.provider != provider:
                raise ValueError(f"settings.{name}: only allowed for provider: {provider}")
        if self.provider == "fake":
            for name in ("api_key_env", "seed_supported"):
                if name in self.settings.model_fields_set:
                    raise ValueError(f"settings.{name}: not allowed for provider: fake")
        return self

    @property
    def api_key_env_name(self) -> str | None:
        """The env var holding this Model's API key; None for the Fake rater."""
        if self.provider == "fake":
            return None
        return self.settings.api_key_env or _DEFAULT_KEY_ENV[self.provider]

    @property
    def fake_settings(self) -> FakeSettings:
        """The ``fake`` settings, defaults when omitted."""
        return self.fake or FakeSettings()

    @field_validator("model")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a pinned model version, not empty")
        if value.strip().lower().endswith("latest"):
            raise ValueError(f"{value!r} is not pinned; name an exact model version")
        return value


class MediaProfile(_Strict):
    """The one canonical encoding every Clip is re-encoded to at ingest."""

    height: Annotated[StrictInt, Field(gt=0)] = 480
    video_kbps: Annotated[StrictInt, Field(gt=0)] = 400
    audio_kbps: Annotated[StrictInt, Field(gt=0)] = 64
    fps: Annotated[StrictInt, Field(gt=0)] = 25

    @field_validator("height")
    @classmethod
    def _even_height(cls, value: int) -> int:
        if value % 2:
            raise ValueError("must be even (H.264 yuv420p needs an even frame height)")
        return value


class RetryPolicy(_Strict):
    """Retries after a ``transient`` Rater result (story 2.1), separate from ``max_retries``.

    The k-th transient attempt of a Trial waits ``min(backoff_max_s, backoff_initial_s *
    2^(k-1))`` seconds times a jitter in [0.5, 1.0] derived from its attempt seed.
    Both backoff values must be finite.
    """

    transient_retries: Annotated[StrictInt, Field(ge=0)] = 3
    backoff_initial_s: Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)] = 2
    backoff_max_s: Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)] = 60

    @model_validator(mode="after")
    def _max_at_least_initial(self) -> RetryPolicy:
        if self.backoff_max_s < self.backoff_initial_s:
            raise ValueError("backoff_max_s must be at least backoff_initial_s")
        return self


class SessionConfig(_Strict):
    practice_clips: Annotated[StrictInt, Field(ge=0)] = 2
    repeats: Annotated[StrictInt, Field(ge=1)] = 3
    max_retries: Annotated[StrictInt, Field(ge=0)] = 2
    pairing: Literal["all_pairs"] = "all_pairs"
    retry: RetryPolicy = Field(
        default_factory=RetryPolicy,
        description="Transient-error retries and backoff; study.yaml only (no per-Test "
        "override).",
    )


class SessionOverrides(_Strict):
    """Per-Test overrides of ``study.yaml`` ``session``; unset keys inherit."""

    practice_clips: Annotated[StrictInt, Field(ge=0)] | None = None
    repeats: Annotated[StrictInt, Field(ge=1)] | None = None
    max_retries: Annotated[StrictInt, Field(ge=0)] | None = None
    pairing: Literal["all_pairs"] | None = None


class LeakTolerance(_Strict):
    """Allowed per-Condition differences; resolution and fps must match exactly."""

    duration_s: Annotated[StrictFloat, Field(ge=0)]
    loudness_lufs: Annotated[StrictFloat, Field(ge=0)]


class Thresholds(_Strict):
    persona_fidelity_min: Annotated[StrictFloat, Field(ge=0, le=1)]
    invalid_rate_max: Annotated[StrictFloat, Field(ge=0, le=1)]
    leak_tolerance: LeakTolerance


QuotaLevels = Annotated[list[StrictStr], Field(min_length=1)]


class Quotas(_Strict):
    age_band: QuotaLevels
    gender: QuotaLevels
    cultural_region: QuotaLevels
    robot_experience: QuotaLevels

    @field_validator("age_band", "gender", "cultural_region", "robot_experience")
    @classmethod
    def _levels_unique(cls, value: list[str]) -> list[str]:
        for level in value:
            if not level.strip():
                raise ValueError("levels must be non-empty strings")
        return _unique(value, "level")


class PersonaFrame(_Strict):
    big_five: Literal["all_32"] = "all_32"
    nars_bands: Annotated[list[Literal["low", "high"]], Field(min_length=1)] = Field(
        default_factory=lambda: ["low", "high"]
    )
    quotas: Quotas

    @field_validator("nars_bands")
    @classmethod
    def _bands_unique(cls, value: list[str]) -> list[str]:
        return _unique(value, "band")


InstrumentName = Annotated[StrictStr, Field(pattern=_NAME_PATTERN)]


class StudyConfig(_Strict):
    schema_version: SchemaVersion
    seed: Annotated[StrictInt, Field(ge=0, lt=2**63)]
    instruments: list[InstrumentName]
    models: Annotated[list[ModelConfig], Field(min_length=1)]
    media: MediaProfile = Field(default_factory=MediaProfile)
    session: SessionConfig = Field(default_factory=SessionConfig)
    concurrency: Annotated[StrictInt, Field(ge=1)] = 4
    thresholds: Thresholds
    personas: PersonaFrame

    @field_validator("instruments")
    @classmethod
    def _instruments_unique(cls, value: list[str]) -> list[str]:
        return _unique(value, "instrument")

    @field_validator("models")
    @classmethod
    def _model_ids_unique(cls, value: list[ModelConfig]) -> list[ModelConfig]:
        _unique([m.id for m in value], "model id")
        return value

    def model_by_id(self, model_id: str) -> ModelConfig:
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(model_id)


# --------------------------------------------------------------------------- tests/*.yaml


AnswerValue = StrictInt | StrictStr
ClipId = Annotated[StrictStr, Field(pattern=_CLIP_ID_PATTERN)]


class PracticeExample(_Strict):
    """A Practice clip (or pair) with the intended answer shown to the Model."""

    instrument: InstrumentName
    clips: Annotated[list[ClipId], Field(min_length=1, max_length=2)]
    answer: dict[StrictStr, AnswerValue]


class TestConfig(_Strict):
    __test__ = False  # not a pytest test class

    schema_version: SchemaVersion
    test: Annotated[StrictStr, Field(pattern=_TEST_NAME_PATTERN)]
    kind: Literal["pilot", "screening", "main"]
    instruments: Annotated[list[InstrumentName], Field(min_length=1)]
    models: list[Annotated[StrictStr, Field(pattern=_MODEL_ID_PATTERN)]] | None = None
    clips: list[ClipId] = Field(default_factory=list)
    practice: list[PracticeExample] = Field(default_factory=list)
    session: SessionOverrides = Field(default_factory=SessionOverrides)

    @field_validator("instruments")
    @classmethod
    def _instruments_unique(cls, value: list[str]) -> list[str]:
        return _unique(value, "instrument")

    @field_validator("models")
    @classmethod
    def _models_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            if not value:
                raise ValueError("must list at least one model id, or be omitted for all")
            _unique(value, "model id")
        return value

    @field_validator("clips")
    @classmethod
    def _clips_unique(cls, value: list[str]) -> list[str]:
        return _unique(value, "clip")

    def model_ids(self, study: StudyConfig) -> list[str]:
        """The Test's Model ids; all of the Study's Models when ``models`` is omitted."""
        return list(self.models) if self.models is not None else [m.id for m in study.models]

    def effective_session(self, study: StudyConfig) -> SessionConfig:
        """``study.yaml`` ``session`` with this Test's overrides applied."""
        overrides = self.session.model_dump(exclude_none=True)
        return study.session.model_copy(update=overrides)


# --------------------------------------------------------------------------- instruments


class Anchors(_Strict):
    low: Annotated[StrictStr, Field(min_length=1)]
    high: Annotated[StrictStr, Field(min_length=1)]


class ItemDef(_Strict):
    id: Annotated[StrictStr, Field(pattern=_NAME_PATTERN)]
    type: Literal["likert", "pairwise", "free_text"]
    text: Annotated[StrictStr, Field(min_length=1)]
    points: Annotated[StrictInt, Field(ge=2, le=11)] | None = None
    anchors: Anchors | None = None
    options: list[Annotated[StrictStr, Field(min_length=1)]] | None = None

    @model_validator(mode="after")
    def _fields_fit_type(self) -> ItemDef:
        if self.type == "likert":
            if self.points is None:
                raise ValueError("likert item needs 'points'")
            if self.anchors is None:
                raise ValueError("likert item needs 'anchors' (low, high)")
        elif self.points is not None or self.anchors is not None:
            raise ValueError(f"{self.type} item takes no 'points' or 'anchors'")
        if self.type == "pairwise":
            if self.options is None:
                self.options = ["A", "B"]
            if len(self.options) != 2 or self.options[0] == self.options[1]:
                raise ValueError("pairwise 'options' must be two distinct labels, e.g. [A, B]")
        elif self.options is not None:
            raise ValueError(f"{self.type} item takes no 'options'")
        return self

    def answer_schema(self) -> dict[str, Any]:
        """JSON Schema of one valid answer to this Item."""
        if self.type == "likert":
            return {"type": "integer", "minimum": 1, "maximum": self.points}
        if self.type == "pairwise":
            return {"type": "string", "enum": list(self.options or [])}
        return {"type": "string", "minLength": 1}


class InstrumentDef(_Strict):
    schema_version: SchemaVersion
    name: InstrumentName
    version: Annotated[StrictStr, Field(min_length=1)]
    draft: StrictBool = Field(
        default=False,
        description="A draft Instrument may not be used in kind: main Tests.",
    )
    instructions: Annotated[StrictStr, Field(min_length=1)]
    prompt_variants: dict[StrictStr, Annotated[StrictStr, Field(min_length=1)]]
    items: Annotated[list[ItemDef], Field(min_length=1)]

    @field_validator("prompt_variants")
    @classmethod
    def _has_default(cls, value: dict[str, str]) -> dict[str, str]:
        if "default" not in value:
            raise ValueError("must contain a 'default' variant")
        return value

    @field_validator("items")
    @classmethod
    def _items_consistent(cls, value: list[ItemDef]) -> list[ItemDef]:
        _unique([item.id for item in value], "item id")
        kinds = {item.type == "pairwise" for item in value}
        if len(kinds) > 1:
            raise ValueError("an Instrument is either all pairwise items or has none")
        return value

    @property
    def pairwise(self) -> bool:
        """True when the Instrument compares two Clips (all its Items are pairwise)."""
        return self.items[0].type == "pairwise"

    def response_schema(self) -> dict[str, Any]:
        """JSON Schema of a valid answer object, keyed by Item id."""
        return {
            "type": "object",
            "properties": {item.id: item.answer_schema() for item in self.items},
            "required": [item.id for item in self.items],
            "additionalProperties": False,
        }

    def answer_problem(self, answer: Any) -> str | None:
        """Why ``answer`` fails ``response_schema()``, as ``"<item>: <reason>"``; None if valid."""
        return _schema_problem(self.response_schema(), answer, "")


def _schema_problem(schema: dict[str, Any], value: Any, field: str) -> str | None:
    """Check ``value`` against the JSON Schema subset ``response_schema()`` emits."""
    where = field or "answer"
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return f"{where}: must be a mapping"
        props = schema["properties"]
        for key in value:
            if key not in props:
                return f"{field + '.' if field else ''}{key}: not an item of this Instrument"
        for key in schema["required"]:
            if key not in value:
                return f"{field + '.' if field else ''}{key}: field required"
        for key, sub in props.items():
            problem = _schema_problem(sub, value[key], f"{field + '.' if field else ''}{key}")
            if problem:
                return problem
        return None
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{where}: must be an integer"
        if not schema["minimum"] <= value <= schema["maximum"]:
            return f"{where}: must be from {schema['minimum']} to {schema['maximum']}"
        return None
    if not isinstance(value, str):
        return f"{where}: must be a string"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where}: must be one of {', '.join(schema['enum'])}"
    if len(value) < schema.get("minLength", 0):
        return f"{where}: must not be empty"
    return None


# --------------------------------------------------------------------------- Persona card wording

# Words a card must never show: they are labels, kept only in panel/personas/index.json.
# Whole words plus trait stems (agreeable, neurotic, introverted, open-minded, ...).
_LABEL_PATTERNS = (
    *TRAITS,
    r"agreeab\w*",
    r"conscientious\w*",
    r"neurotic\w*",
    r"extr[ao]ver\w*",
    r"introver\w*",
    r"open-minded\w*",
    "high",
    "low",
    "nars",
)
_LABEL_WORDS = re.compile(r"\b(" + "|".join(_LABEL_PATTERNS) + r")\b", re.I)

Sentence = Annotated[StrictStr, Field(min_length=1)]


def _check_sentence(value: str) -> str:
    if value != value.strip() or len(value.splitlines()) != 1:
        raise ValueError("must be one line with no leading or trailing whitespace")
    found = _LABEL_WORDS.search(value)
    if found:
        raise ValueError(f"must describe behaviour only; contains the label {found.group(0)!r}")
    return value


class TraitWording(_Strict):
    high: Sentence
    low: Sentence

    @field_validator("high", "low")
    @classmethod
    def _sentence(cls, value: str) -> str:
        return _check_sentence(value)


class TraitsWording(_Strict):
    openness: TraitWording
    conscientiousness: TraitWording
    extraversion: TraitWording
    agreeableness: TraitWording
    neuroticism: TraitWording


class LevelPhrases(_Strict):
    """Phrase inserted for each quota level; every level in the frame needs one."""

    age_band: dict[StrictStr, Sentence] = Field(default_factory=dict)
    gender: dict[StrictStr, Sentence] = Field(default_factory=dict)
    cultural_region: dict[StrictStr, Sentence] = Field(default_factory=dict)
    robot_experience: dict[StrictStr, Sentence] = Field(default_factory=dict)

    @field_validator("age_band", "gender", "cultural_region", "robot_experience")
    @classmethod
    def _phrases(cls, value: dict[str, str]) -> dict[str, str]:
        for phrase in value.values():
            _check_sentence(phrase)
        return value


class CardWording(_Strict):
    """The approved Persona card wording (package ``templates/persona_card/wording.yaml``)."""

    voice: Literal["second_person"]
    demographic: Sentence
    traits: TraitsWording
    nars: dict[Literal["low", "high"], Sentence]
    level_phrases: LevelPhrases = Field(default_factory=LevelPhrases)

    @field_validator("demographic")
    @classmethod
    def _demographic_template(cls, value: str) -> str:
        _check_sentence(value)
        try:
            parsed = [p for p in string.Formatter().parse(value) if p[1] is not None]
        except ValueError as err:
            raise ValueError(f"not a valid line template: {err}") from err
        for _, field, spec, conversion in parsed:
            if spec or conversion:
                raise ValueError(
                    f"placeholder {{{field}}} may not have a conversion or format spec"
                )
        fields = [p[1] for p in parsed]
        unknown = sorted(set(fields) - set(QUOTA_ATTRIBUTES))
        if unknown:
            raise ValueError(f"unknown placeholder(s) {', '.join(unknown)}")
        missing = [a for a in QUOTA_ATTRIBUTES if a not in fields]
        if missing:
            raise ValueError(f"missing placeholder(s) {', '.join(missing)}")
        return value

    @field_validator("nars")
    @classmethod
    def _nars_sentences(cls, value: dict[str, str]) -> dict[str, str]:
        for sentence in value.values():
            _check_sentence(sentence)
        return value


# --------------------------------------------------------------------------- prices.yaml


class ModelPrice(_Strict):
    """Prices in USD per million tokens plus the token formula of ``core.cost`` (story 1.9)."""

    input_usd_per_mtok: DecimalStr
    output_usd_per_mtok: DecimalStr
    media_tokens_per_s: DecimalStr = Field(
        default=Decimal("300"), description="Input tokens per second of Clip media."
    )
    chars_per_token: PositiveDecimalStr = Field(
        default=Decimal("4"), description="Characters of rendered request text per input token."
    )


class PricesConfig(_Strict):
    schema_version: SchemaVersion
    models: dict[Annotated[StrictStr, Field(pattern=_MODEL_ID_PATTERN)], ModelPrice] = Field(
        json_schema_extra={"additionalProperties": False}
    )
