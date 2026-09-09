"""Write the shared component projection into the schema-10 derived tables.

Called from `refresh.rebuild_projection` inside the one transaction that also
publishes the generation, so a reader that pins a generation row always finds the
matching `analysis_components` rows and never a half-written set. One committed
current projection only — every refresh deletes and repopulates.
"""

from __future__ import annotations

import json
import sqlite3

from .components import ComponentProjection

_INSERT_COMPONENT = """
INSERT INTO analysis_components(
    component_id, origin_component_id, generation_token, lens,
    origin_kind, origin_anchor, entry_id, role, bucket,
    signed_coefficient, scale, currency,
    represented_start, represented_end, date_precision, period,
    category_key, merchant_key, merchant_display, account,
    source_location, evidence_sha256, decision_seqs, nets, eligible
) VALUES (
    :component_id, :origin_component_id, :generation_token, :lens,
    :origin_kind, :origin_anchor, :entry_id, :role, :bucket,
    :signed_coefficient, :scale, :currency,
    :represented_start, :represented_end, :date_precision, :period,
    :category_key, :merchant_key, :merchant_display, :account,
    :source_location, :evidence_sha256, :decision_seqs, :nets, :eligible
)
"""


def materialize_components(
    connection: sqlite3.Connection,
    generation_token: str,
    projection: ComponentProjection,
) -> int:
    """Replace the committed component projection with ``projection``.

    The caller owns the transaction. Returns the number of component rows written.
    """
    connection.execute("DELETE FROM component_provenance")
    connection.execute("DELETE FROM component_source_refs")
    connection.execute("DELETE FROM component_relationships")
    connection.execute("DELETE FROM analysis_components")

    components = []
    source_refs = []
    provenance = []
    for component in projection.components:
        component_id = component.component_id(generation_token)
        components.append(
            {
                "component_id": component_id,
                "origin_component_id": component.origin_component_id,
                "generation_token": generation_token,
                "lens": component.lens,
                "origin_kind": component.origin_kind,
                "origin_anchor": component.origin_anchor,
                "entry_id": component.entry_id,
                "role": component.role,
                "bucket": component.bucket,
                "signed_coefficient": component.coefficient,
                "scale": component.scale,
                "currency": component.currency,
                "represented_start": component.represented_start,
                "represented_end": component.represented_end,
                "date_precision": component.precision,
                "period": component.period,
                "category_key": component.category_key,
                "merchant_key": component.merchant_key,
                "merchant_display": component.merchant_display,
                "account": component.account,
                "source_location": component.source_location,
                "evidence_sha256": component.evidence_sha256,
                "decision_seqs": json.dumps(list(component.decision_seqs)),
                "nets": int(component.nets),
                "eligible": 1,
            }
        )
        source_refs.append(
            (component_id, component.evidence_sha256, component.source_location)
        )
        provenance.append(
            (
                component_id,
                generation_token,
                component.entry_id,
                component.source_account_id,
                component.account_source_type,
                component.payment_channel,
            )
        )

    connection.executemany(_INSERT_COMPONENT, components)
    connection.executemany(
        "INSERT OR IGNORE INTO component_source_refs("
        "component_id, evidence_sha256, locator) VALUES (?, ?, ?)",
        source_refs,
    )
    # Ledger provenance for the card cash-conversion contract: which account the
    # movement came from and which channel it settles through. Written from the
    # same projection in the same transaction, so a component can never be read
    # with a channel that belongs to a different generation.
    connection.executemany(
        "INSERT OR REPLACE INTO component_provenance("
        "component_id, generation_token, entry_id, source_account_id, "
        "account_source_type, payment_channel) VALUES (?, ?, ?, ?, ?, ?)",
        provenance,
    )
    return len(components)
