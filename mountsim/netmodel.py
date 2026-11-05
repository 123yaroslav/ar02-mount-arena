"""Модель сети с виртуальным временем.

Одно TCP-соединение, у каждого направления своя очередь передачи. Время доставки
сообщения складывается из ожидания свободного канала, передачи с учетом заголовков
пакетов, половины RTT, случайного джиттера и среднего штрафа за потери. Потери считаются
средним значением, чтобы у метрики было мало шума. Джиттер случайный и непредсказуемый
для протокола (с версии v2): без него кандидат мог откалибровать канал и рассчитывать
время доставки точно, чего в настоящей сети не бывает.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

MSS = 1448  # полезная нагрузка TCP-сегмента, байт
PACKET_OVERHEAD = 66  # заголовки Ethernet, IP и TCP на пакет, байт
FRAME_OVERHEAD = 8  # рамка сообщения протокола, байт
MIN_RTO = 0.2  # минимальный таймаут повторной передачи в Linux, с
JITTER = 0.3  # добавка к задержке в одну сторону, доля от половины RTT, равномерно от 0 до JITTER


@dataclass(frozen=True)
class Profile:
    rtt: float  # время туда и обратно, с
    loss: float  # доля потерянных пакетов
    bandwidth: float  # пропускная способность канала, байт/с

    @classmethod
    def from_spec(cls, spec: dict) -> Profile:
        return cls(
            rtt=spec["rtt_ms"] / 1000.0,
            loss=float(spec["loss"]),
            bandwidth=spec["bandwidth_mbit"] * 1e6 / 8,
        )

    @property
    def rto(self) -> float:
        return max(MIN_RTO, 2 * self.rtt)

    @property
    def effective_bandwidth(self) -> float:
        """Потолок одного TCP-потока при потерях по формуле Матиса."""
        if self.loss <= 0 or self.rtt <= 0:
            return self.bandwidth
        return min(self.bandwidth, 1.22 * MSS / (self.rtt * math.sqrt(self.loss)))


class Link:
    """Одно направление соединения."""

    def __init__(self, profile: Profile, rng: random.Random | None = None):
        self.profile = profile
        self.rng = rng
        self.busy_until = 0.0
        self.last_arrival = 0.0
        self.wire_bytes = 0
        self.payload_bytes = 0
        self.messages = 0

    def transmit(self, now: float, size: int) -> float:
        p = self.profile
        packets = max(1, math.ceil((size + FRAME_OVERHEAD) / MSS))
        wire = size + FRAME_OVERHEAD + packets * PACKET_OVERHEAD
        start = max(now, self.busy_until)
        tx = wire / p.effective_bandwidth
        self.busy_until = start + tx
        # потеря одного из последних трех пакетов ждет RTO, остальные чинит быстрая повторная передача
        penalty = p.loss * (max(0, packets - 3) * p.rtt + min(packets, 3) * p.rto)
        jitter = self.rng.random() * JITTER * p.rtt / 2 if self.rng is not None else 0.0
        arrival = max(start + tx + p.rtt / 2 + jitter + penalty, self.last_arrival)
        self.last_arrival = arrival
        self.wire_bytes += wire
        self.payload_bytes += size
        self.messages += 1
        return arrival
