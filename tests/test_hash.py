from app.services.pdf_parser import compute_transaction_hash


def test_same_inputs_produce_same_hash():
    h1 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "REF123")
    h2 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "REF123")
    assert h1 == h2


def test_different_reference_ids_produce_different_hashes():
    h1 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "REF001")
    h2 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "REF002")
    assert h1 != h2


def test_empty_vs_nonempty_reference_id_differ():
    h1 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "")
    h2 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "REF001")
    assert h1 != h2


def test_description_is_case_insensitive():
    h1 = compute_transaction_hash("2026-01-15", "GRAB", 12.50, "DBS / POSB", "")
    h2 = compute_transaction_hash("2026-01-15", "grab", 12.50, "DBS / POSB", "")
    assert h1 == h2


def test_different_amounts_produce_different_hashes():
    h1 = compute_transaction_hash("2026-01-15", "Grab", 12.50, "DBS / POSB", "")
    h2 = compute_transaction_hash("2026-01-15", "Grab", 12.51, "DBS / POSB", "")
    assert h1 != h2
