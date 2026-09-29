"""EIP-712 signing helper.

Order placement on Omni authenticates by session cookie and does NOT require a
signed order intent, so the range bot never calls into this module. It is
provided for the on-chain flows the venue does sign locally - notably the USDC
deposit permit (`/api/on_chain/permit`, an EIP-2612 permit) - and as a clean,
tested primitive for signing any EIP-712 typed payload.

The domain and message types below are the standard EIP-2612 `Permit`. Adjust
`verifyingContract`, `chainId`, and the type struct to match whatever payload
the target endpoint expects; the signing mechanics do not change.
"""

from __future__ import annotations

from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data


def sign_typed_data(private_key: str, typed_data: dict[str, Any]) -> dict[str, str]:
    """Sign a full EIP-712 typed-data document.

    Args:
        private_key: hex private key (with or without 0x prefix).
        typed_data: a dict with keys ``types``, ``domain``, ``primaryType``,
            ``message`` - the standard EIP-712 structure.

    Returns:
        dict with ``signature`` (0x hex), ``r``, ``s``, ``v``, and the
        recovered ``address``, so callers can attach whatever the endpoint wants.
    """
    signable = encode_typed_data(full_message=typed_data)
    acct = Account.from_key(private_key)
    signed = acct.sign_message(signable)
    return {
        "address": acct.address,
        "signature": "0x" + signed.signature.hex().removeprefix("0x"),
        "r": hex(signed.r),
        "s": hex(signed.s),
        "v": hex(signed.v),
    }


def build_permit_typed_data(
    *,
    token_name: str,
    verifying_contract: str,
    chain_id: int,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
    version: str = "1",
) -> dict[str, Any]:
    """Build an EIP-2612 `Permit` typed-data document (USDC-style gasless
    approval). This is the shape Omni's deposit-permit flow signs."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Permit": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "domain": {
            "name": token_name,
            "version": version,
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "primaryType": "Permit",
        "message": {
            "owner": owner,
            "spender": spender,
            "value": value,
            "nonce": nonce,
            "deadline": deadline,
        },
    }


if __name__ == "__main__":
    # Demonstration with a throwaway key. Signs a permit and recovers the signer.
    demo_key = "0x" + "11" * 32
    demo_owner = Account.from_key(demo_key).address
    typed = build_permit_typed_data(
        token_name="USD Coin",
        verifying_contract="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        chain_id=1,
        owner=demo_owner,
        spender="0x0000000000000000000000000000000000000001",
        value=1_000_000,
        nonce=0,
        deadline=4_102_444_800,
    )
    out = sign_typed_data(demo_key, typed)
    print("signer :", demo_owner)
    print("address:", out["address"])
    print("sig    :", out["signature"][:24], "...")
    assert out["address"] == demo_owner, "recovered address mismatch"
    print("OK: recovered address matches signer")
