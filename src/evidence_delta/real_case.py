from __future__ import annotations

from datetime import UTC, datetime

from evidence_delta.schemas import AssertionInput, DocumentInput
from evidence_delta.service import EvidenceService
from evidence_delta.worker import RecomputeWorker

COMPLAINT_URI = "https://www.justice.gov/iso/opa/resources/628201351145721158286.pdf"
INDICTMENT_URI = (
    "https://www.justice.gov/usao-ma/pr/"
    "federal-grand-jury-indicts-two-men-obstruction-justice-boston-marathon-bombing"
)
FBI_HISTORY_URI = "https://www.fbi.gov/history/cases-and-criminals/boston-marathon-bombing"
SENTENCING_URI = (
    "https://www.justice.gov/usao-ma/pr/"
    "dias-kadyrbayev-sentenced-six-years-impeding-boston-marathon-bombing-investigation"
)


def event(
    entity_id: str,
    occurred_at: str,
    kind: str,
    value: str,
    locator: str,
    source_text: str,
    *,
    precision: str = "EXACT",
) -> AssertionInput:
    return AssertionInput(
        entity_id=entity_id,
        occurred_at=datetime.fromisoformat(occurred_at).replace(tzinfo=UTC),
        kind=kind,
        value=value,
        time_precision=precision,
        source_locator=locator,
        source_text=source_text,
    )


def boston_obstruction_documents() -> list[DocumentInput]:
    """Curated public records for the narrow evidence-disposal sub-case.

    Complaint and indictment assertions remain explicitly labeled as
    allegations. Court-established assertions come only from the later DOJ
    sentencing record, after a guilty plea or jury verdict.
    """

    complaint = DocumentInput(
        filename="2013-05-01-kadyrbayev-tazhayakov-criminal-complaint.pdf",
        source_type="criminal_complaint",
        source_uri=COMPLAINT_URI,
        assertions=[
            event(
                "dias-kadyrbayev",
                "2013-04-19T00:43:00",
                "COMPLAINT_DIGITAL_RECORD",
                "The affidavit reports a cellphone exchange with Dzhokhar between "
                "8:43 and 8:48 p.m. EDT on April 18.",
                "affidavit:paragraph-23",
                "Cellphone analysis reflects that the texts were sent between 8:43 p.m. "
                "and 8:48 p.m. EST.",
                precision="MINUTE",
            ),
            event(
                "dzhokhar-tsarnaev",
                "2013-04-19T00:43:00",
                "COMPLAINT_DIGITAL_RECORD",
                "The affidavit reports Dzhokhar replied during the cellphone exchange "
                "with Kadyrbayev.",
                "affidavit:paragraph-23",
                "Tsarnaev's return texts contained jokes and an invitation to take items "
                "from his room.",
                precision="MINUTE",
            ),
            event(
                "dzhokhar-dorm-room",
                "2013-04-18T22:00:00",
                "COMPLAINT_WITNESS_ACCOUNT",
                "The affidavit says Kadyrbayev, Tazhayakov, and Phillipos entered the "
                "UMass Dartmouth dorm room that evening.",
                "affidavit:paragraph-24",
                "The three men met at the UMass Dartmouth campus and went to Tsarnaev's "
                "dormitory room.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-18T22:00:00",
                "COMPLAINT_OBJECT_OBSERVATION",
                "The affidavit describes a backpack containing opened fireworks that "
                "appeared emptied of powder.",
                "affidavit:paragraph-24",
                "They noticed a backpack containing fireworks. The fireworks had been "
                "opened and emptied of powder.",
                precision="WINDOW",
            ),
            event(
                "laptop-computer",
                "2013-04-18T22:00:00",
                "COMPLAINT_OBJECT_TRANSFER",
                "The affidavit alleges that Tsarnaev's laptop was removed from the dorm room.",
                "affidavit:paragraph-27",
                "They removed the backpack, Vaseline, and Tsarnaev's laptop from the "
                "dormitory room.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-19T04:00:00",
                "COMPLAINT_OBJECT_DISPOSAL",
                "The affidavit alleges that the backpack and fireworks were discarded in "
                "the apartment-complex dumpster early on April 19.",
                "affidavit:paragraph-28",
                "Kadyrbayev decided to throw away the backpack with the fireworks inside "
                "and Tazhayakov agreed.",
                precision="WINDOW",
            ),
            event(
                "dias-kadyrbayev",
                "2013-04-19T04:00:00",
                "COMPLAINT_ALLEGED_ACTION",
                "The affidavit attributes the physical disposal of the backpack to Kadyrbayev.",
                "affidavit:paragraph-28",
                "Kadyrbayev was the one who threw the backpack in the garbage.",
                precision="WINDOW",
            ),
            event(
                "azamat-tazhayakov",
                "2013-04-19T04:00:00",
                "COMPLAINT_ALLEGED_AGREEMENT",
                "The affidavit alleges that Tazhayakov agreed to discard the backpack.",
                "affidavit:paragraph-28",
                "Kadyrbayev decided to throw away the backpack with the fireworks inside "
                "and Tazhayakov agreed.",
                precision="WINDOW",
            ),
        ],
    )

    indictment = DocumentInput(
        filename="2013-08-08-obstruction-indictment-announcement.html",
        source_type="indictment_announcement",
        source_uri=INDICTMENT_URI,
        assertions=[
            event(
                "dias-kadyrbayev",
                "2013-04-18T22:00:00",
                "INDICTMENT_ALLEGATION",
                "The indictment alleges Kadyrbayev received a message suggesting he take "
                "items from Tsarnaev's room.",
                "press-release:paragraph-3",
                "Kadyrbayev received a text message suggesting that he go to Tsarnaev's "
                "room and take what was there.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-18T22:00:00",
                "INDICTMENT_ALLEGED_TRANSFER",
                "The indictment alleges the group removed a backpack containing fireworks "
                "from the dorm room.",
                "press-release:paragraph-3",
                "The group removed several items, including a backpack containing fireworks.",
                precision="WINDOW",
            ),
            event(
                "laptop-computer",
                "2013-04-18T22:00:00",
                "INDICTMENT_ALLEGED_TRANSFER",
                "The indictment alleges the group removed Tsarnaev's laptop computer.",
                "press-release:paragraph-3",
                "The group removed several items, including Tsarnaev's laptop computer.",
                precision="WINDOW",
            ),
            event(
                "new-bedford-apartment",
                "2013-04-18T22:00:00",
                "INDICTMENT_ALLEGED_LOCATION",
                "The indictment alleges the removed items were brought to the New "
                "Bedford apartment.",
                "press-release:paragraph-3",
                "They brought the removed items to Kadyrbayev and Tazhayakov's apartment "
                "in New Bedford.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-19T04:00:00",
                "INDICTMENT_ALLEGED_DISPOSAL",
                "The indictment alleges the backpack was placed in a garbage bag and then "
                "in the apartment dumpster.",
                "press-release:paragraph-4",
                "The backpack was placed in a garbage bag and put in a trash dumpster.",
                precision="WINDOW",
            ),
            event(
                "azamat-tazhayakov",
                "2013-04-19T04:00:00",
                "INDICTMENT_ALLEGED_AGREEMENT",
                "The indictment alleges Tazhayakov knew of and agreed with the disposal.",
                "press-release:paragraph-4",
                "Kadyrbayev acted with Tazhayakov's knowledge and agreement.",
                precision="WINDOW",
            ),
        ],
    )

    fbi_history = DocumentInput(
        filename="fbi-boston-marathon-case-history.html",
        source_type="agency_case_history",
        source_uri=FBI_HISTORY_URI,
        assertions=[
            event(
                "boston-marathon-investigation",
                "2013-04-15T16:00:00",
                "AGENCY_CASE_CONTEXT",
                "The FBI history records that the Boston Marathon attack occurred on "
                "April 15, 2013 and initiated the investigation.",
                "case-history:paragraph-1",
                "On April 15, 2013, two explosives were detonated near the Marathon "
                "finish line.",
                precision="DAY",
            ),
            event(
                "fbi-public-identification",
                "2013-04-18T16:00:00",
                "AGENCY_PUBLIC_RELEASE",
                "The FBI history records that photos and video of two suspects were "
                "released three days after the bombing.",
                "case-history:paragraph-2",
                "Three days after the bombing, the FBI released photos and video of "
                "the two suspects.",
                precision="DAY",
            ),
        ],
    )

    sentencing = DocumentInput(
        filename="2015-06-02-kadyrbayev-sentencing-record.html",
        source_type="court_outcome_record",
        source_uri=SENTENCING_URI,
        assertions=[
            event(
                "dias-kadyrbayev",
                "2013-04-18T22:00:00",
                "COURT_ESTABLISHED_ACTION",
                "The sentencing record states that Kadyrbayev entered the dorm room and "
                "removed the laptop and backpack.",
                "press-release:paragraph-4",
                "Kadyrbayev removed Tsarnaev's laptop and a backpack containing fireworks, "
                "Vaseline, and a thumb drive.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-18T22:00:00",
                "COURT_ESTABLISHED_TRANSFER",
                "The sentencing record places the backpack's removal at the dorm room on "
                "the evening of April 18.",
                "press-release:paragraph-4",
                "Kadyrbayev removed Tsarnaev's laptop and a backpack containing fireworks, "
                "Vaseline, and a thumb drive.",
                precision="WINDOW",
            ),
            event(
                "laptop-computer",
                "2013-04-18T22:00:00",
                "COURT_ESTABLISHED_TRANSFER",
                "The sentencing record places the laptop's removal at the dorm room on the "
                "evening of April 18.",
                "press-release:paragraph-4",
                "Kadyrbayev removed Tsarnaev's laptop and a backpack containing fireworks, "
                "Vaseline, and a thumb drive.",
                precision="WINDOW",
            ),
            event(
                "azamat-tazhayakov",
                "2013-04-19T04:00:00",
                "COURT_ESTABLISHED_AGREEMENT",
                "The sentencing record states Kadyrbayev and Tazhayakov agreed to get rid "
                "of the backpack.",
                "press-release:paragraph-5",
                "Kadyrbayev and Tazhayakov agreed that they should get rid of Tsarnaev's backpack.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-19T04:00:00",
                "COURT_ESTABLISHED_DISPOSAL",
                "The sentencing record states Kadyrbayev put the backpack in a trash bag "
                "and threw it into the dumpster.",
                "press-release:paragraph-5",
                "Kadyrbayev threw the entire bag into the garbage dumpster in his "
                "apartment complex.",
                precision="WINDOW",
            ),
            event(
                "laptop-computer",
                "2013-04-19T04:00:00",
                "COURT_ESTABLISHED_CONCEALMENT",
                "The sentencing record states Kadyrbayev kept and continued concealing the laptop.",
                "press-release:paragraph-5",
                "Kadyrbayev decided to keep Tsarnaev's laptop computer and continued "
                "to conceal it.",
                precision="WINDOW",
            ),
            event(
                "backpack",
                "2013-04-26T16:00:00",
                "COURT_ESTABLISHED_RECOVERY",
                "Federal agents recovered the backpack from a New Bedford landfill after "
                "a two-day search.",
                "press-release:paragraph-6",
                "After a two-day search, federal agents found Tsarnaev's backpack in a New "
                "Bedford landfill.",
                precision="DAY",
            ),
            event(
                "azamat-tazhayakov",
                "2014-07-01T16:00:00",
                "COURT_OUTCOME_REPORTED",
                "The sentencing record reports that a federal jury found Tazhayakov guilty "
                "of conspiracy and obstruction in July 2014.",
                "press-release:paragraph-7",
                "In July 2014, Tazhayakov was found guilty by a federal jury in Boston.",
                precision="MONTH",
            ),
            event(
                "dias-kadyrbayev",
                "2015-06-02T16:00:00",
                "COURT_OUTCOME_SENTENCE",
                "Kadyrbayev was sentenced to six years after pleading guilty to conspiracy "
                "and obstruction.",
                "press-release:paragraph-1",
                "Kadyrbayev was sentenced to six years in prison and had previously "
                "pleaded guilty.",
                precision="DAY",
            ),
        ],
    )

    return [complaint, indictment, fbi_history, sentencing]


def build_boston_obstruction_case(
    service: EvidenceService,
    worker: RecomputeWorker,
) -> dict:
    documents = boston_obstruction_documents()
    case = service.create_case("Boston evidence-disposal case / official public record")
    document_ids = [
        service.ingest_document(case.id, document).document_id for document in documents
    ]
    worker.run_until_idle()
    proof = service.case_proof(case.id)
    assertion_total = sum(len(document.assertions) for document in documents)
    court_established = sum(
        assertion.kind.startswith("COURT_")
        for document in documents
        for assertion in document.assertions
    )
    return {
        "template_id": "boston-obstruction-public-record-v1",
        "case_id": case.id,
        "document_ids": document_ids,
        "official_sources": len(documents),
        "assertions": assertion_total,
        "court_established_assertions": court_established,
        "materialized_timelines": proof["artifacts"]["total"],
        "equivalent_to_full_rebuild": proof["equivalent_to_full_rebuild"],
        "source_uris": [document.source_uri for document in documents],
    }
