from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    """Pinned Fairground testnet deployment used by the official web client.

    The documentation currently exposes neither a deployment manifest nor a
    downloadable Python write SDK.  These values were therefore taken from the
    official production frontend and verified read-only against Arbitrum
    Sepolia on 2026-07-19.  The executor verifies them again on every start.
    """

    chain_id: int
    perps_contract: str
    multicall_forwarder: str
    collateral_token: str
    collateral_decimals: int
    collateral_proxy_code_hash: str
    collateral_implementation: str
    collateral_implementation_code_hash: str
    price_oracle: str
    oracle_proxy_code_hash: str
    oracle_implementation: str
    oracle_implementation_code_hash: str
    proxy_code_hash: str
    multicall_code_hash: str
    selector_storage_namespace: str
    open_reduce_facet_code_hash: str
    cancel_facet_code_hash: str
    getter_facet_code_hash: str
    market_config_facet_code_hash: str


ARBITRUM_SEPOLIA = DeploymentManifest(
    chain_id=421614,
    # Live UI (2026-09-10 HAR): new diamond + new test USDC. Old
    # 0x924e82AA… / 0xAa6112ab… still exist, so a stale pin keeps verifying
    # and then every open/reduce reverts.
    perps_contract="0xFc371e4fCb222f67f90E8156867D6cdb626cb7C9",
    multicall_forwarder="0x415fF910e58ed23b4493620EE12A7b195a016820",
    collateral_token="0x4E1156749dd156D06dCE5baC0f8f5A43792C9EDb",  # gitleaks:allow — public contract
    collateral_decimals=6,
    collateral_proxy_code_hash="a9a0e35ef58350a560173c241d616d51e1ca9c7050bdf403e9914dc0733cc22b",
    collateral_implementation="0xB3027f76ABa178cB3e45EC01c70d5F2ae970ecf0",
    collateral_implementation_code_hash="74db35d4753beac14486b402d34b23a163ced66f243225f63631f643c16c5ba7",
    price_oracle="0x47D1706cbaFEDc5D29eB68923C13F7289e4B7C9C",
    oracle_proxy_code_hash="f83adf10f05f94fef4ed0a49925b1677b93b1b2ee0af655887d4d4f6d6937604",
    oracle_implementation="0xfb21d6D11cf08944E8CdF2Df4008c5aB442CC033",
    oracle_implementation_code_hash="8824dcc4b98fd3d286cbc7ad4ccda2192d47db3bae57dab7e5d9f0d58b8fe907",
    proxy_code_hash="9f9a3e08cf7a33f73ccd80d3edab7f6db6374bff59ac349e0f400eed23ce7484",
    multicall_code_hash="0b949075b694511b0f1fea4788fb014b58059e178f6b1e595e1bfb922db909e2",
    selector_storage_namespace="be498a3b14fd79a48c3c9af86ff14271ed497d09071013334028d28e7791db20",
    # open/reduce: 0xAf7aCd4c77D870E47443F6C61F1d73867347B2fc
    open_reduce_facet_code_hash="32e7765755034981efe383a26de0f750006ee1d89a4ed8777d509c3fe65679c0",
    # cancel: 0x5cAee0e06d155033D358cC1761b26055f452D2BB
    cancel_facet_code_hash="a5864c3ba6e513425cb1a309e6295e24999afac0077ed5db4ce6bce84472408e",
    getter_facet_code_hash="be6ff36d1a4b645db6ec8d3de4caf29b34695cd34fd827f41e191c716bf6ef92",
    # market config: 0x0804CFF711B5EF500F087857c8f360eFd303A058
    market_config_facet_code_hash="7f7afceefcb109ce822e2fb9daa225773425c24c1ad3013eb331c34caaa9845a",
)


# Function signatures used both for calldata and selector/facet verification.
OPEN_ORDER_SIGNATURE = "openOrder((uint64,uint48,uint48,bool,uint32,uint32,uint32))"
REDUCE_ORDER_SIGNATURE = "reduceOrder((uint64,uint40,uint56,uint32,uint8))"
CANCEL_ORDER_SIGNATURE = "cancelOrder(uint64,uint40,bool)"
