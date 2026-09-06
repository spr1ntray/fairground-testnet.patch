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
    perps_contract="0x924e82AA0c62a44EA7148945Ae078100cFE2b595",
    multicall_forwarder="0x415fF910e58ed23b4493620EE12A7b195a016820",
    collateral_token="0xAa6112ab9850fd185632Ef0F4029487b9572D279",  # gitleaks:allow — public contract
    collateral_decimals=6,
    collateral_proxy_code_hash="a9a0e35ef58350a560173c241d616d51e1ca9c7050bdf403e9914dc0733cc22b",
    collateral_implementation="0x1616113f481897b4b14E487664177a100C28adaE",
    collateral_implementation_code_hash="1daa557ab506a4cfb2d289c63137927e50985e9001c68640f78be48d2ae90f06",
    price_oracle="0x47D1706cbaFEDc5D29eB68923C13F7289e4B7C9C",
    oracle_proxy_code_hash="f83adf10f05f94fef4ed0a49925b1677b93b1b2ee0af655887d4d4f6d6937604",
    oracle_implementation="0xfb21d6D11cf08944E8CdF2Df4008c5aB442CC033",
    oracle_implementation_code_hash="8824dcc4b98fd3d286cbc7ad4ccda2192d47db3bae57dab7e5d9f0d58b8fe907",
    proxy_code_hash="cee396e4cfe0080376a54773d9ed651bcee16a1a3eeda2613099cc51b812190d",
    multicall_code_hash="0b949075b694511b0f1fea4788fb014b58059e178f6b1e595e1bfb922db909e2",
    # The proxy dispatches selector => facet from mapping slot
    # bytes32(uint256(namespace) - 1).  Pinning the proxy bytecode alone would
    # not detect a facet upgrade, so the write facets are checked separately.
    selector_storage_namespace="be498a3b14fd79a48c3c9af86ff14271ed497d09071013334028d28e7791db20",
    # Fairground performed an on-chain EIP-2535 DiamondCut upgrade at blocks
    # 288852049..288852359 on 2026-07-18.  The production frontend still pins
    # this proxy and the same ABI selectors.  Current facets were independently
    # resolved from selector storage and their runtime bytecode re-hashed.
    # open/reduce: 0x1091b1A5a3b6a36E738D9ccfCD977E707C310afD
    open_reduce_facet_code_hash="b1fc6cd7385449c4bb03a49475223d5b246f457c424c4d2a967f1c14b46f5e9f",
    # cancel: 0x3dF2EC1562F21A01032d3695ce3ae39B70203a60
    cancel_facet_code_hash="61c535c76bb5ebbf6267607fae2b6839f5a02ac3f38e90d01a0f61d336d0ca6f",
    getter_facet_code_hash="b9b5cc86ce496895ad0b40fd4535f3c67b1b7842a76b40d3b8a580ff1c607c6d",
    # market config: 0x19d88a294B938e4Cb3D5342c110DEa5c7B302F02
    market_config_facet_code_hash="80912b6c1566cf6c157c3cc1b5e53ccab7054f3509e211265c6ee2aa7a79eaf7",
)


# Function signatures used both for calldata and selector/facet verification.
OPEN_ORDER_SIGNATURE = "openOrder((uint64,uint48,uint48,bool,uint32,uint32,uint32))"
REDUCE_ORDER_SIGNATURE = "reduceOrder((uint64,uint40,uint56,uint32,uint8))"
CANCEL_ORDER_SIGNATURE = "cancelOrder(uint64,uint40,bool)"
