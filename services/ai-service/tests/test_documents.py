from datetime import UTC, datetime
from decimal import Decimal

from app import documents
from app.config import Settings
from sfo_events.payloads import MenuItemUpsertedPayload, ReviewSubmittedPayload


def _item(**over) -> MenuItemUpsertedPayload:
    return MenuItemUpsertedPayload.model_validate(
        {
            "menu_item_id": "m-1",
            "restaurant_id": "r-1",
            "restaurant_name": "Desi Dastarkhwan",
            "restaurant_status": "active",
            "cuisine_type": "Pakistani",
            "city": "Islamabad",
            "lat": 33.72,
            "lng": 73.05,
            "category_id": "c-1",
            "category_name": "Rice",
            "name": "Chicken Biryani",
            "description": "Dum-cooked sela rice.",
            "price": Decimal("690.00"),
            "dietary_tags": ["halal", "gluten_free"],
            "spice_level": 2,
            "calories": 820,
            "is_available": True,
            "in_stock": True,
            "source_version": 3,
            **over,
        }
    )


def test_menu_item_text_carries_its_context() -> None:
    doc = documents.menu_item(_item())
    assert doc.text == (
        "Chicken Biryani — Rice at Desi Dastarkhwan (Pakistani, Islamabad). "
        "Dum-cooked sela rice. Spicy; halal, gluten-free. About 820 kcal. PKR 690."
    )
    assert doc.payload["location"] == {"lat": 33.72, "lon": 73.05}
    assert doc.payload["price"] == 690.0
    assert doc.id == documents.menu_item(_item(name="Renamed")).id  # same entity, same point


def test_chunking_overlaps_and_covers_everything() -> None:
    words = [f"w{i}" for i in range(500)]
    chunks = documents.chunk_words(" ".join(words))
    assert len(chunks) == 3
    assert [len(c.split()) for c in chunks] == [220, 220, 140]
    assert chunks[1].split()[:40] == chunks[0].split()[-40:]
    assert chunks[-1].split()[-1] == "w499"
    assert documents.chunk_words("short review") == ["short review"]


def test_long_review_becomes_chunks_with_context() -> None:
    review = ReviewSubmittedPayload(
        order_id="o-1",
        restaurant_id="r-1",
        restaurant_name="Desi Dastarkhwan",
        rating=5,
        text=" ".join(["zabardast"] * 300),
        item_ids=["m-1"],
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        avg_rating=4.5,
        review_count=2,
        source_version=4,
    )
    docs = documents.review(review, {"city": "Islamabad", "location": {"lat": 1, "lon": 2}})
    assert len(docs) == 2
    assert all(d.text.startswith("Review of Desi Dastarkhwan, 5/5: ") for d in docs)
    assert {d.payload["chunks"] for d in docs} == {2}
    assert docs[0].payload["city"] == "Islamabad"
    assert len({d.id for d in docs}) == 2


def test_collection_suffix_names_model_and_revision() -> None:
    s = Settings(database_url="postgresql+asyncpg://x/y", service_name="ai-service")
    assert s.index_suffix == "bge_base_en_v1_5_r2"


def test_filter_words_fold_plurals_the_same_on_both_sides() -> None:
    assert documents.words("Thai / Pan-Asian") == ["asian", "pan", "thai"]
    assert documents.words("Chinese-Pakistani") == ["chinese", "pakistani"]
    # Stored "green chillies" and an excluded "chilli" meet at the same word.
    assert documents.stem("chillies") == documents.stem("chilli") == "chilli"
    assert documents.stem("nuts") == "nut" and documents.stem("tomatoes") == "tomato"
    assert documents.stem("glass") == "glass"
    assert documents.words(None, "") == []


def test_item_payload_carries_its_filter_words() -> None:
    doc = documents.menu_item(
        _item(name="Beef Nihari", description="Slow-cooked with green chillies.")
    )
    assert {"beef", "nihari", "rice", "chilli", "slow", "cooked"} <= set(doc.payload["words"])
    assert doc.payload["cuisine_words"] == ["pakistani"]
    # Words are payload only: the embedded text is unchanged.
    assert "words" not in doc.text
