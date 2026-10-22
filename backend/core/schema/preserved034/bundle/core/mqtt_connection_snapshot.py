"""Import-safe immutable broker connection inputs for owned protocol workers."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MqttConnectionSnapshot:
    host: str = field(repr=False)
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    use_tls: bool
