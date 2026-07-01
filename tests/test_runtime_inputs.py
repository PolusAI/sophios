from sophios.runtime_inputs import normalize_job_inputs


def test_normalize_job_inputs_uses_concrete_file_format() -> None:
    job_inputs = {
        "ligand": {
            "class": "File",
            "location": "/data/benzamidine.sdf",
            "format": [
                "edam:format_1476",
                "edam:format_3814",
                "edam:format_3816",
            ],
        },
        "nested": [
            {
                "class": "File",
                "location": "/data/receptor.pdb",
                "format": [
                    "edam:format_1476",
                    "edam:format_3814",
                ],
            }
        ],
    }

    normalized = normalize_job_inputs({}, job_inputs)

    assert normalized["ligand"]["format"] == "edam:format_3814"
    assert normalized["nested"][0]["format"] == "edam:format_1476"


def test_normalize_job_inputs_uses_edam_pdbqt_format() -> None:
    job_inputs = {
        "ligand": {
            "class": "File",
            "basename": "ligand.pdbqt",
            "format": [
                "edam:format_1476",
                "edam:format_4036",
            ],
        },
    }

    normalized = normalize_job_inputs({}, job_inputs)

    assert normalized["ligand"]["format"] == "edam:format_4036"


def test_normalize_job_inputs_removes_unresolved_format_list() -> None:
    job_inputs = {
        "ligand": {
            "class": "File",
            "location": "/data/ligand.mol",
            "format": [
                "edam:format_1476",
                "edam:format_3815",
            ],
        },
    }

    normalized = normalize_job_inputs({}, job_inputs)

    assert "format" not in normalized["ligand"]
