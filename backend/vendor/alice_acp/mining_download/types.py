from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MiningDownloadContentContract:
    primary_entry_label: str = "Start Mining"
    product_mode_label: str = "Alice Rewarded Mining Mode"
    requires_miner_rvn_wallet: bool = False
    gpu_default_copy: str = "GPU RVN/KAWPOW mining is the default local mining task."
    cpu_optional_copy: str = "CPU RandomX mining is optional concurrent idle work."
    ai_preemption_copy: str = "Admitted AI tasks can throttle or pause mining by demand."
    collection_wallet_copy: str = (
        "Alice assigns the collection wallet inside each signed session."
    )
    reward_state_copy: str = (
        "Rewards are shadow-only; live rewards and live settlement remain disabled "
        "until later gates close."
    )
    setup_items: tuple[str, ...] = (
        "Alice passport",
        "supported GPU backend",
        "Alice signed mining session",
    )

    def __post_init__(self) -> None:
        required = (
            self.primary_entry_label,
            self.product_mode_label,
            self.gpu_default_copy,
            self.cpu_optional_copy,
            self.ai_preemption_copy,
            self.collection_wallet_copy,
            self.reward_state_copy,
        )
        if any(not value for value in required):
            raise ValueError("download content copy fields must be non-empty")
        if self.requires_miner_rvn_wallet:
            raise ValueError("miner RVN wallet must not be required")
        if any(not value for value in self.setup_items):
            raise ValueError("setup items must be non-empty")

    def render_plain_text(self) -> str:
        lines = (
            self.primary_entry_label,
            self.product_mode_label,
            self.gpu_default_copy,
            self.cpu_optional_copy,
            self.ai_preemption_copy,
            self.collection_wallet_copy,
            self.reward_state_copy,
            "Setup:",
            *self.setup_items,
        )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "primary_entry_label": self.primary_entry_label,
            "product_mode_label": self.product_mode_label,
            "requires_miner_rvn_wallet": self.requires_miner_rvn_wallet,
            "gpu_default_copy": self.gpu_default_copy,
            "cpu_optional_copy": self.cpu_optional_copy,
            "ai_preemption_copy": self.ai_preemption_copy,
            "collection_wallet_copy": self.collection_wallet_copy,
            "reward_state_copy": self.reward_state_copy,
            "setup_items": self.setup_items,
        }
