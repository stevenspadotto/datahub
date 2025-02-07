import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Union

from pydantic import validator
from pydantic.fields import Field

import datahub.metadata.schema_classes as models
from datahub.configuration.common import ConfigModel
from datahub.configuration.config_loader import load_config_file
from datahub.emitter.mce_builder import (
    datahub_guid,
    get_sys_time,
    make_group_urn,
    make_user_urn,
)
from datahub.ingestion.api.decorators import (  # SourceCapability,; capability,
    SupportStatus,
    config_class,
    platform_name,
    support_status,
)
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit, UsageStatsWorkUnit

logger = logging.getLogger(__name__)

valid_status: models.StatusClass = models.StatusClass(removed=False)

# This needed to map path presents in inherits, contains, values, and related_terms to terms' optional id
path_vs_id: Dict[str, Optional[str]] = {}

auditStamp = models.AuditStampClass(
    time=get_sys_time(), actor="urn:li:corpUser:restEmitter"
)


class Owners(ConfigModel):
    users: Optional[List[str]]
    groups: Optional[List[str]]


class GlossaryTermConfig(ConfigModel):
    id: Optional[str]
    name: str
    description: str
    term_source: Optional[str]
    source_ref: Optional[str]
    source_url: Optional[str]
    owners: Optional[Owners]
    inherits: Optional[List[str]]
    contains: Optional[List[str]]
    values: Optional[List[str]]
    related_terms: Optional[List[str]]
    custom_properties: Optional[Dict[str, str]]


class GlossaryNodeConfig(ConfigModel):
    id: Optional[str]
    name: str
    description: str
    owners: Optional[Owners]
    terms: Optional[List[GlossaryTermConfig]]
    nodes: Optional[List["GlossaryNodeConfig"]]


GlossaryNodeConfig.update_forward_refs()


class DefaultConfig(ConfigModel):
    """Holds defaults for populating fields in glossary terms"""

    source: str
    owners: Owners
    url: Optional[str] = None
    source_type: Optional[str] = "INTERNAL"


class BusinessGlossarySourceConfig(ConfigModel):
    file: str = Field(description="Path to business glossary file to ingest.")
    enable_auto_id: bool = Field(
        description="Generate id field from GlossaryNode and GlossaryTerm's name field",
        default=False,
    )


class BusinessGlossaryConfig(DefaultConfig):
    version: str
    nodes: Optional[List[GlossaryNodeConfig]]
    terms: Optional[List[GlossaryTermConfig]]

    @validator("version")
    def version_must_be_1(cls, v):
        if v != "1":
            raise ValueError("Only version 1 is supported")
        return v


def create_id(path: List[str], default_id: Optional[str], enable_auto_id: bool) -> str:
    if default_id is not None:
        return default_id  # No need to create id from path as default_id is provided

    id_: str = ".".join(path)
    if enable_auto_id:
        id_ = datahub_guid({"path": id_})
    return id_


def make_glossary_node_urn(
    path: List[str], default_id: Optional[str], enable_auto_id: bool
) -> str:
    if default_id is not None and default_id.startswith("urn:li:glossaryNode:"):
        logger.debug(
            f"node's default_id({default_id}) is in urn format for path {path}. Returning same as urn"
        )
        return default_id

    return "urn:li:glossaryNode:" + create_id(path, default_id, enable_auto_id)


def make_glossary_term_urn(
    path: List[str], default_id: Optional[str], enable_auto_id: bool
) -> str:
    if default_id is not None and default_id.startswith("urn:li:glossaryTerm:"):
        logger.debug(
            f"term's default_id({default_id}) is in urn format for path {path}. Returning same as urn"
        )
        return default_id

    return "urn:li:glossaryTerm:" + create_id(path, default_id, enable_auto_id)


def get_owners(owners: Owners) -> models.OwnershipClass:
    owners_meta: List[models.OwnerClass] = []
    if owners.users is not None:
        owners_meta = owners_meta + [
            models.OwnerClass(
                owner=make_user_urn(o),
                type=models.OwnershipTypeClass.DEVELOPER,
            )
            for o in owners.users
        ]
    if owners.groups is not None:
        owners_meta = owners_meta + [
            models.OwnerClass(
                owner=make_group_urn(o),
                type=models.OwnershipTypeClass.DEVELOPER,
            )
            for o in owners.groups
        ]
    return models.OwnershipClass(owners=owners_meta)


def get_mces(
    glossary: BusinessGlossaryConfig, ingestion_config: BusinessGlossarySourceConfig
) -> List[models.MetadataChangeEventClass]:
    events: List[models.MetadataChangeEventClass] = []
    path: List[str] = []
    root_owners = get_owners(glossary.owners)

    if glossary.nodes:
        for node in glossary.nodes:
            events += get_mces_from_node(
                node,
                path + [node.name],
                parentNode=None,
                parentOwners=root_owners,
                defaults=glossary,
                ingestion_config=ingestion_config,
            )

    if glossary.terms:
        for term in glossary.terms:
            events += get_mces_from_term(
                term,
                path + [term.name],
                parentNode=None,
                parentOwnership=root_owners,
                defaults=glossary,
                ingestion_config=ingestion_config,
            )

    return events


def get_mce_from_snapshot(snapshot: Any) -> models.MetadataChangeEventClass:
    return models.MetadataChangeEventClass(proposedSnapshot=snapshot)


def get_mces_from_node(
    glossaryNode: GlossaryNodeConfig,
    path: List[str],
    parentNode: Optional[str],
    parentOwners: models.OwnershipClass,
    defaults: DefaultConfig,
    ingestion_config: BusinessGlossarySourceConfig,
) -> List[models.MetadataChangeEventClass]:
    node_urn = make_glossary_node_urn(
        path, glossaryNode.id, ingestion_config.enable_auto_id
    )
    node_info = models.GlossaryNodeInfoClass(
        definition=glossaryNode.description,
        parentNode=parentNode,
        name=glossaryNode.name,
    )
    node_owners = parentOwners
    if glossaryNode.owners is not None:
        assert glossaryNode.owners is not None
        node_owners = get_owners(glossaryNode.owners)

    node_snapshot = models.GlossaryNodeSnapshotClass(
        urn=node_urn,
        aspects=[node_info, node_owners, valid_status],
    )
    mces = [get_mce_from_snapshot(node_snapshot)]
    if glossaryNode.nodes:
        for node in glossaryNode.nodes:
            mces += get_mces_from_node(
                node,
                path + [node.name],
                parentNode=node_urn,
                parentOwners=node_owners,
                defaults=defaults,
                ingestion_config=ingestion_config,
            )

    if glossaryNode.terms:
        for term in glossaryNode.terms:
            mces += get_mces_from_term(
                glossaryTerm=term,
                path=path + [term.name],
                parentNode=node_urn,
                parentOwnership=node_owners,
                defaults=defaults,
                ingestion_config=ingestion_config,
            )
    return mces


def get_mces_from_term(
    glossaryTerm: GlossaryTermConfig,
    path: List[str],
    parentNode: Optional[str],
    parentOwnership: models.OwnershipClass,
    defaults: DefaultConfig,
    ingestion_config: BusinessGlossarySourceConfig,
) -> List[models.MetadataChangeEventClass]:
    term_urn = make_glossary_term_urn(
        path, glossaryTerm.id, ingestion_config.enable_auto_id
    )
    aspects: List[
        Union[
            models.GlossaryTermInfoClass,
            models.GlossaryRelatedTermsClass,
            models.OwnershipClass,
            models.StatusClass,
            models.GlossaryTermKeyClass,
            models.BrowsePathsClass,
        ]
    ] = []
    term_info = models.GlossaryTermInfoClass(
        definition=glossaryTerm.description,
        termSource=glossaryTerm.term_source  # type: ignore
        if glossaryTerm.term_source is not None
        else defaults.source_type,
        sourceRef=glossaryTerm.source_ref
        if glossaryTerm.source_ref
        else defaults.source,
        sourceUrl=glossaryTerm.source_url if glossaryTerm.source_url else defaults.url,
        parentNode=parentNode,
        customProperties=glossaryTerm.custom_properties,
        name=glossaryTerm.name,
    )
    aspects.append(term_info)

    is_a = None
    has_a = None
    values: Union[None, List[str]] = None
    related_terms: Union[None, List[str]] = None
    if glossaryTerm.inherits is not None:
        assert glossaryTerm.inherits is not None
        is_a = [
            make_glossary_term_urn(
                [term],
                default_id=path_vs_id.get(term),
                enable_auto_id=ingestion_config.enable_auto_id,
            )
            for term in glossaryTerm.inherits
        ]
    if glossaryTerm.contains is not None:
        assert glossaryTerm.contains is not None
        has_a = [
            make_glossary_term_urn(
                [term],
                default_id=path_vs_id.get(term),
                enable_auto_id=ingestion_config.enable_auto_id,
            )
            for term in glossaryTerm.contains
        ]
    if glossaryTerm.values is not None:
        assert glossaryTerm.values is not None
        values = [
            make_glossary_term_urn(
                [term],
                default_id=path_vs_id.get(term),
                enable_auto_id=ingestion_config.enable_auto_id,
            )
            for term in glossaryTerm.values
        ]
    if glossaryTerm.related_terms is not None:
        assert glossaryTerm.related_terms is not None
        related_terms = [
            make_glossary_term_urn(
                [term],
                default_id=path_vs_id.get(term),
                enable_auto_id=ingestion_config.enable_auto_id,
            )
            for term in glossaryTerm.related_terms
        ]

    if (
        is_a is not None
        or has_a is not None
        or values is not None
        or related_terms is not None
    ):
        related_term_aspect = models.GlossaryRelatedTermsClass(
            isRelatedTerms=is_a,
            hasRelatedTerms=has_a,
            values=values,
            relatedTerms=related_terms,
        )
        aspects.append(related_term_aspect)

    ownership: models.OwnershipClass = parentOwnership
    if glossaryTerm.owners is not None:
        assert glossaryTerm.owners is not None
        ownership = get_owners(glossaryTerm.owners)
    aspects.append(ownership)

    term_browse = models.BrowsePathsClass(paths=["/" + "/".join(path)])
    aspects.append(term_browse)

    term_snapshot: models.GlossaryTermSnapshotClass = models.GlossaryTermSnapshotClass(
        urn=term_urn,
        aspects=aspects,
    )
    return [get_mce_from_snapshot(term_snapshot)]


def populate_path_vs_id(glossary: BusinessGlossaryConfig) -> None:
    path: List[str] = []

    def _process_child_terms(parent_node: GlossaryNodeConfig, path: List[str]) -> None:
        path_vs_id[".".join(path + [parent_node.name])] = parent_node.id

        if parent_node.terms:
            for term in parent_node.terms:
                path_vs_id[".".join(path + [parent_node.name] + [term.name])] = term.id

        if parent_node.nodes:
            for node in parent_node.nodes:
                _process_child_terms(node, path + [parent_node.name])

    if glossary.nodes:
        for node in glossary.nodes:
            _process_child_terms(node, path)

    if glossary.terms:
        for term in glossary.terms:
            path_vs_id[".".join(path + [term.name])] = term.id


@platform_name("Business Glossary")
@config_class(BusinessGlossarySourceConfig)
@support_status(SupportStatus.CERTIFIED)
@dataclass
class BusinessGlossaryFileSource(Source):
    """
    This plugin pulls business glossary metadata from a yaml-formatted file. An example of one such file is located in the examples directory [here](https://github.com/datahub-project/datahub/blob/master/metadata-ingestion/examples/bootstrap_data/business_glossary.yml).
    """

    config: BusinessGlossarySourceConfig
    report: SourceReport = field(default_factory=SourceReport)

    @classmethod
    def create(cls, config_dict, ctx):
        config = BusinessGlossarySourceConfig.parse_obj(config_dict)
        return cls(ctx, config)

    def load_glossary_config(self, file_name: str) -> BusinessGlossaryConfig:
        config = load_config_file(file_name)
        glossary_cfg = BusinessGlossaryConfig.parse_obj(config)
        return glossary_cfg

    def get_workunits(self) -> Iterable[Union[MetadataWorkUnit, UsageStatsWorkUnit]]:
        glossary_config = self.load_glossary_config(self.config.file)
        populate_path_vs_id(glossary_config)
        for mce in get_mces(glossary_config, ingestion_config=self.config):
            wu = MetadataWorkUnit(f"{mce.proposedSnapshot.urn}", mce=mce)
            self.report.report_workunit(wu)
            yield wu

    def get_report(self):
        return self.report

    def close(self):
        pass
