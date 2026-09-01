from pathlib import Path

from ingestion_pipeline.sources.base import read_table


def test_csv_source_detects_delimiter_and_pads_short_rows(tmp_path: Path) -> None:
    path = tmp_path / "input.csv"
    path.write_text("Código;Nombre;Precio\nA-1;Crema;10\nA-2;Jabón\n", encoding="utf-8")

    table = read_table(path)

    assert table.headers == ["Código", "Nombre", "Precio"]
    assert table.rows[1] == ["A-2", "Jabón", None]
