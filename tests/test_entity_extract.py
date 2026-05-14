from app.pipeline.entity_extract import extract_entities


def test_ticker_extraction_ignores_sources_and_capitalized_words():
    entities = extract_entities("Why are UK prices rising more quickly? BBC Business RSS")

    assert entities.tickers == []
    assert "英国" in entities.countries
