from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES = (
    "AdjustToasterOvenTemperature",
    "AdjustWaterTemperature",
    "CloseBlenderLid",
    "CloseCabinet",
    "CloseDishwasher",
    "CloseDrawer",
    "CloseElectricKettleLid",
    "CloseFridge",
    "CloseFridgeDrawer",
    "CloseMicrowave",
    "CloseOven",
    "CloseStandMixerHead",
    "CloseToasterOvenDoor",
    "CoffeeServeMug",
    "CoffeeSetupMug",
    "NavigateKitchen",
    "OpenBlenderLid",
    "OpenCabinet",
    "OpenDishwasher",
    "OpenDrawer",
    "OpenElectricKettleLid",
    "OpenFridge",
    "OpenFridgeDrawer",
    "OpenMicrowave",
    "OpenOven",
    "OpenStandMixerHead",
    "OpenToasterOvenDoor",
    "PickPlaceCabinetToCounter",
    "PickPlaceCounterToBlender",
    "PickPlaceCounterToCabinet",
    "PickPlaceCounterToDrawer",
    "PickPlaceCounterToMicrowave",
    "PickPlaceCounterToOven",
    "PickPlaceCounterToSink",
    "PickPlaceCounterToStandMixer",
    "PickPlaceCounterToStove",
    "PickPlaceCounterToToasterOven",
    "PickPlaceDrawerToCounter",
    "PickPlaceFridgeDrawerToShelf",
    "PickPlaceFridgeShelfToDrawer",
    "PickPlaceMicrowaveToCounter",
    "PickPlaceSinkToCounter",
    "PickPlaceStoveToCounter",
    "PickPlaceToasterOvenToCounter",
    "PickPlaceToasterToCounter",
    "PreheatOven",
    "SlideDishwasherRack",
    "SlideOvenRack",
    "SlideToasterOvenRack",
    "StartCoffeeMachine",
    "TurnOffMicrowave",
    "TurnOffSinkFaucet",
    "TurnOffStove",
    "TurnOnBlender",
    "TurnOnElectricKettle",
    "TurnOnMicrowave",
    "TurnOnSinkFaucet",
    "TurnOnStove",
    "TurnOnToaster",
    "TurnOnToasterOven",
    "TurnSinkSpout",
    "MakeIcedCoffee",
    "CheesyBread",
    "LowerHeat",
    "PackDessert",
)


ATOMIC_SKILL_CONTRACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AtomicSkillContract:
    schema_version: int
    skill_id: str
    level: str
    category: str
    instruction_template: str
    input_schema: dict[str, Any]
    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    termination: dict[str, str]
    executor: dict[str, str]
    action_space: dict[str, str]
    origin: str
    official_dataset_anchor: str | None
    evidence_status: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["preconditions"] = list(self.preconditions)
        value["postconditions"] = list(self.postconditions)
        return value


@dataclass(frozen=True)
class TaskAtomicSkill:
    name: str
    family: str
    instruction: str
    expected_postcondition: str
    origin: str
    official_dataset_anchor: str | None = None

    def contract(self) -> AtomicSkillContract:
        return AtomicSkillContract(
            schema_version=ATOMIC_SKILL_CONTRACT_SCHEMA_VERSION,
            skill_id=self.name,
            level="atomic",
            category=self.family,
            instruction_template=self.instruction,
            input_schema={
                "type": "object",
                "required": [
                    "task",
                    "observation",
                    "subtask",
                    "task_atomic_skill_id",
                    "task_atomic_skill_instruction",
                ],
                "properties": {
                    "task": {"type": "object"},
                    "observation": {"type": "object"},
                    "subtask": {"type": "string", "minLength": 1},
                    "task_atomic_skill_id": {
                        "type": "string",
                        "const": self.name,
                    },
                    "task_atomic_skill_instruction": {
                        "type": "string",
                        "const": self.instruction,
                    },
                },
            },
            preconditions=(
                "bound_node_instruction_is_non_empty",
                "current_observation_is_available",
                "physical_environment_is_preserved",
            ),
            postconditions=(self.expected_postcondition,),
            termination={
                "success": "expected_postcondition_verified",
                "failure": "executor_error_or_safety_stop",
                "bound": "runtime_horizon_and_pipeline_action_chunk_limit",
            },
            executor={
                "backend": ("luban.backends.skills.XiaomiRemotePolicyBackend"),
                "binding": "AtomicSpatialSkillExecutionPipeline",
            },
            action_space={
                "type": "policy_generated_robot_action_chunks",
                "authority": "configured_policy_backend",
            },
            origin=self.origin,
            official_dataset_anchor=self.official_dataset_anchor,
            evidence_status=("integrated_contract_without_paired_causal_effect_claim"),
        )


def _official(
    name: str,
    family: str,
    instruction: str,
    expected_postcondition: str,
) -> TaskAtomicSkill:
    if name not in ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES:
        raise ValueError(f"unknown RoboCasa365 official atomic task: {name}")
    return TaskAtomicSkill(
        name=name,
        family=family,
        instruction=instruction,
        expected_postcondition=expected_postcondition,
        origin="robocasa365_official_atomic_task",
        official_dataset_anchor=name,
    )


def _derived(
    name: str,
    family: str,
    instruction: str,
    expected_postcondition: str,
) -> TaskAtomicSkill:
    return TaskAtomicSkill(
        name=name,
        family=family,
        instruction=instruction,
        expected_postcondition=expected_postcondition,
        origin="composite_seen_success_condition",
    )


TASK_ATOMIC_SKILLS = (
    _official(
        "NavigateKitchen",
        "navigation",
        (
            "Navigate to the named kitchen fixture or work area and stop with the "
            "target visible and reachable."
        ),
        "The robot is safely positioned at the named fixture with a usable view.",
    ),
    _official(
        "OpenCabinet",
        "articulation",
        "Open the named cabinet door far enough to access its contents.",
        "The named cabinet is visibly open and accessible.",
    ),
    _official(
        "CloseCabinet",
        "articulation",
        "Close the named cabinet door completely.",
        "The named cabinet door is visibly closed.",
    ),
    _official(
        "OpenDrawer",
        "articulation",
        "Open the named left or right drawer far enough to access the target.",
        "The correct drawer is visibly open and accessible.",
    ),
    _official(
        "CloseDrawer",
        "articulation",
        "Close the named drawer completely.",
        "The named drawer is visibly closed.",
    ),
    _official(
        "OpenFridge",
        "articulation",
        "Open the refrigerator door far enough to access the required shelf.",
        "The refrigerator is visibly open and the target shelf is accessible.",
    ),
    _official(
        "CloseFridge",
        "articulation",
        "Close the refrigerator door completely.",
        "The refrigerator door is visibly closed.",
    ),
    _official(
        "OpenMicrowave",
        "articulation",
        "Open the microwave door far enough to access the cavity.",
        "The microwave cavity is visibly accessible.",
    ),
    _official(
        "CloseMicrowave",
        "articulation",
        "Close the microwave door completely.",
        "The microwave door is visibly closed.",
    ),
    _official(
        "OpenDishwasher",
        "articulation",
        "Open the dishwasher door far enough to access the rack.",
        "The dishwasher rack area is accessible.",
    ),
    _official(
        "CloseDishwasher",
        "articulation",
        "Close the dishwasher door completely.",
        "The dishwasher door is visibly closed.",
    ),
    _official(
        "SlideDishwasherRack",
        "articulation",
        "Slide the top dishwasher rack in or out as named, then stop.",
        "The top rack reaches the requested in or out state.",
    ),
    _official(
        "PickPlaceCabinetToCounter",
        "pick_place",
        (
            "Pick the named object from the cabinet and place it on the named counter"
            " region."
        ),
        "The correct object is stably on the counter and the gripper is clear.",
    ),
    _official(
        "PickPlaceDrawerToCounter",
        "pick_place",
        (
            "Pick the named object from the drawer and place it on the named counter "
            "region."
        ),
        "The correct object is stably on the counter and the gripper is clear.",
    ),
    _official(
        "PickPlaceCounterToCabinet",
        "pick_place",
        (
            "Pick the named object from the counter and place it inside the named "
            "cabinet region."
        ),
        "The correct object is stably inside the cabinet and the gripper is clear.",
    ),
    _official(
        "PickPlaceCounterToSink",
        "pick_place",
        (
            "Pick the named object from the counter and place it fully inside the "
            "sink basin."
        ),
        "The correct object is inside the sink and the gripper is clear.",
    ),
    _official(
        "PickPlaceSinkToCounter",
        "pick_place",
        (
            "Pick the named object from the sink and place it in the named receptacle"
            " on the counter."
        ),
        "The correct object is in the target receptacle and the gripper is clear.",
    ),
    _official(
        "PickPlaceCounterToMicrowave",
        "pick_place",
        "Pick the named item from the counter and place it fully inside the microwave.",
        "The correct item is stable inside the microwave and the gripper is clear.",
    ),
    _official(
        "PickPlaceCounterToStove",
        "pick_place",
        (
            "Pick the named item and place it in the named pan or on the named stove "
            "burner."
        ),
        "The correct item is stably in the pan or centered on the requested burner.",
    ),
    _official(
        "PickPlaceToasterToCounter",
        "pick_place",
        "Pick the toasted item from the toaster and place it on the named plate.",
        "The toasted item is on the plate and the gripper is clear.",
    ),
    _official(
        "CoffeeSetupMug",
        "appliance_setup",
        "Place the named mug directly under the coffee-machine dispenser.",
        "The mug is aligned in the machine's pouring area and the gripper is clear.",
    ),
    _official(
        "StartCoffeeMachine",
        "button_press",
        "Press the coffee-machine start button once, then move the gripper away.",
        "The coffee machine reports on and the gripper is clear of the button.",
    ),
    _official(
        "TurnOnSinkFaucet",
        "fixture_control",
        "Turn the sink faucet on and move the gripper clear of the handle.",
        "Water is visibly running from the sink faucet.",
    ),
    _official(
        "TurnOffSinkFaucet",
        "fixture_control",
        "Turn the sink faucet off and move the gripper clear of the handle.",
        "Water is visibly no longer running.",
    ),
    _official(
        "TurnSinkSpout",
        "fixture_control",
        "Move the sink spout to the named left, center, or right basin location.",
        "The running-water stream reaches the requested basin location.",
    ),
    _official(
        "AdjustWaterTemperature",
        "fixture_control",
        (
            "Adjust the running-water handle to the named hot or cold setting while "
            "keeping water on."
        ),
        "The faucet remains on at the requested temperature setting.",
    ),
    _official(
        "TurnOnStove",
        "fixture_control",
        "Turn on exactly the named stove burner using its matching knob.",
        "The requested burner is on and the gripper is clear of the knob.",
    ),
    _official(
        "TurnOffStove",
        "fixture_control",
        "Turn off exactly the named stove burner using its matching knob.",
        "The requested burner is off and the gripper is clear of the knob.",
    ),
    _official(
        "TurnOnMicrowave",
        "button_press",
        "Press the microwave start button once, then move the gripper away.",
        "The microwave reports on and the gripper is clear of the button.",
    ),
    _official(
        "TurnOffMicrowave",
        "button_press",
        "Press the microwave stop button once, then move the gripper away.",
        "The microwave reports off and the gripper is clear of the button.",
    ),
    _official(
        "TurnOnToaster",
        "fixture_control",
        "Push the toaster lever down once to start toasting.",
        "The bread remains in a toaster slot and the toaster reports on.",
    ),
    _derived(
        "PlaceObjectInReceptacle",
        "precision_place",
        (
            "Pick the named object and place it fully inside the named bowl, pan, "
            "cup, tupperware, plate, or rack."
        ),
        (
            "The named object is contained by or stably supported on the requested "
            "receptacle and the gripper is clear."
        ),
    ),
    _derived(
        "PlaceObjectInFridge",
        "precision_place",
        (
            "Pick the named bowl or container and place it stably on the requested "
            "refrigerator rack."
        ),
        "The container has stable refrigerator-rack contact and the gripper is clear.",
    ),
    _derived(
        "PlaceObjectOnDishwasherRack",
        "precision_place",
        "Pick the named dish and place it stably on the top dishwasher rack.",
        "The dish has stable top-rack contact and the gripper is clear.",
    ),
    _derived(
        "StackContainers",
        "precision_place",
        (
            "Place the smaller named bowl inside or on top of the larger named bowl "
            "without moving the larger bowl out of its target region."
        ),
        "Both bowls remain in the target region and one is stably nested in the other.",
    ),
    _derived(
        "InsertSlenderObjectInCup",
        "precision_place",
        (
            "Align the named slender object above the cup opening, lower it inside, "
            "release, and move the gripper clear."
        ),
        "The slender object remains inside the cup and the gripper is clear.",
    ),
    _derived(
        "DistributeItemsAcrossContainers",
        "sorting",
        (
            "Place exactly one item of each named type into each target container, "
            "without reusing an item."
        ),
        "Every target container has one item of each required type with no duplicates.",
    ),
    _derived(
        "ScrubSurfaceCoverage",
        "contact_rich",
        (
            "Grasp the sponge, maintain contact with the named surface, sweep across "
            "visibly separated points, then release the sponge."
        ),
        (
            "The sponge contacted multiple separated surface points over a sufficient"
            " sweep and is released."
        ),
    ),
    _derived(
        "StirContents",
        "contact_rich",
        (
            "Keep the tool inside the container and make a small bounded circular "
            "stirring motion while maintaining contact with the contents."
        ),
        (
            "The required contents move within the container under tool contact for "
            "several consecutive checks."
        ),
    ),
    _derived(
        "HoldObjectUnderRunningWater",
        "contact_rich",
        (
            "Place or hold the named object under running faucet water without "
            "dropping it or leaving the sink."
        ),
        "Running water reaches the object for the required continuous duration.",
    ),
    _derived(
        "SweepRunningWaterAcrossBasin",
        "fixture_control",
        (
            "With water running, move the sink spout through left, center, and right "
            "basin locations, observing each location before continuing."
        ),
        "Running water has reached all three basin regions.",
    ),
)


TASK_SKILL_MAP = {
    "DeliverStraw": (
        "NavigateKitchen",
        "OpenDrawer",
        "PickPlaceDrawerToCounter",
        "InsertSlenderObjectInCup",
    ),
    "GetToastedBread": (
        "TurnOnToaster",
        "PickPlaceToasterToCounter",
    ),
    "KettleBoiling": (
        "PickPlaceCounterToStove",
        "TurnOnStove",
    ),
    "LoadDishwasher": (
        "OpenDishwasher",
        "SlideDishwasherRack",
        "PlaceObjectOnDishwasherRack",
        "CloseDishwasher",
    ),
    "PackIdenticalLunches": (
        "DistributeItemsAcrossContainers",
        "PlaceObjectInReceptacle",
    ),
    "PreSoakPan": (
        "PickPlaceCounterToSink",
        "TurnOnSinkFaucet",
    ),
    "PrepareCoffee": (
        "OpenCabinet",
        "PickPlaceCabinetToCounter",
        "CoffeeSetupMug",
        "StartCoffeeMachine",
    ),
    "RinseSinkBasin": (
        "TurnOnSinkFaucet",
        "TurnSinkSpout",
        "SweepRunningWaterAcrossBasin",
    ),
    "ScrubCuttingBoard": ("ScrubSurfaceCoverage",),
    "SearingMeat": (
        "OpenCabinet",
        "PickPlaceCabinetToCounter",
        "PickPlaceCounterToStove",
        "PlaceObjectInReceptacle",
        "TurnOnStove",
    ),
    "SetUpCuttingStation": (
        "OpenDrawer",
        "PickPlaceDrawerToCounter",
        "PlaceObjectInReceptacle",
    ),
    "StackBowlsCabinet": (
        "PickPlaceCounterToCabinet",
        "StackContainers",
    ),
    "SteamInMicrowave": (
        "PickPlaceSinkToCounter",
        "PlaceObjectInReceptacle",
        "PickPlaceCounterToMicrowave",
        "CloseMicrowave",
        "TurnOnMicrowave",
    ),
    "StirVegetables": ("PlaceObjectInReceptacle", "StirContents"),
    "StoreLeftoversInBowl": (
        "PlaceObjectInReceptacle",
        "OpenFridge",
        "PlaceObjectInFridge",
    ),
    "WashLettuce": (
        "PickPlaceCounterToSink",
        "TurnOnSinkFaucet",
        "HoldObjectUnderRunningWater",
    ),
}

# TaskDecomposer keeps stable task-level names for the planner and XR-1 policy.
# Contract-derived knowledge is keyed by versioned directory IDs (P01, M04,
# T04, ...). This table is a retrieval join only; it does not claim that any
# listed directory contract is executable on XR-1.
TASK_SKILL_DIRECTORY_IDS = {
    "NavigateKitchen": ("M08", "M09", "N05"),
    "OpenCabinet": ("A02",),
    "CloseCabinet": ("A02",),
    "OpenDrawer": ("A01",),
    "CloseDrawer": ("A01",),
    "OpenFridge": ("A02",),
    "CloseFridge": ("A02",),
    "OpenMicrowave": ("A02",),
    "CloseMicrowave": ("A02",),
    "OpenDishwasher": ("A02",),
    "CloseDishwasher": ("A02",),
    "SlideDishwasherRack": ("A01",),
    "PickPlaceCabinetToCounter": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceDrawerToCounter": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceCounterToCabinet": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceCounterToSink": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceSinkToCounter": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceCounterToMicrowave": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceCounterToStove": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PickPlaceToasterToCounter": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "CoffeeSetupMug": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "StartCoffeeMachine": ("A04",),
    "TurnOnSinkFaucet": ("A03",),
    "TurnOffSinkFaucet": ("A03",),
    "TurnSinkSpout": ("A03", "M04"),
    "AdjustWaterTemperature": ("A03",),
    "TurnOnStove": ("A03",),
    "TurnOffStove": ("A03",),
    "TurnOnMicrowave": ("A04",),
    "TurnOffMicrowave": ("A04",),
    "TurnOnToaster": ("A04",),
    "PlaceObjectInReceptacle": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PlaceObjectInFridge": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "PlaceObjectOnDishwasherRack": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "StackContainers": ("P01", "P03", "G03", "M04", "I08", "I09", "G01"),
    "InsertSlenderObjectInCup": ("P01", "P03", "G03", "M04", "I10", "G01"),
    "DistributeItemsAcrossContainers": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "ScrubSurfaceCoverage": ("P01", "P11", "G03", "M04", "I08", "T04", "G01"),
    "StirContents": ("P01", "P03", "G03", "M04", "T04", "I01", "G01"),
    "HoldObjectUnderRunningWater": ("P01", "P03", "G03", "M04", "I08", "G01"),
    "SweepRunningWaterAcrossBasin": ("A03", "M04", "T04"),
}


class RoboCasaAtomicSkillCatalog:
    def __init__(
        self,
        skills: tuple[TaskAtomicSkill, ...] = TASK_ATOMIC_SKILLS,
        task_skill_map: Mapping[str, tuple[str, ...]] = TASK_SKILL_MAP,
    ) -> None:
        by_name = {skill.name: skill for skill in skills}
        if not by_name or len(by_name) != len(skills):
            raise ValueError("task atomic skill names must be unique")
        unknown = {
            name
            for names in task_skill_map.values()
            for name in names
            if name not in by_name
        }
        if unknown:
            raise ValueError(
                f"task skill map references unknown skills: {sorted(unknown)}"
            )
        self._skills = by_name
        self._task_skill_map = {
            key: tuple(value) for key, value in task_skill_map.items()
        }

    @staticmethod
    def task_name(task: Any) -> str | None:
        if isinstance(task, str):
            return task.rsplit("/", 1)[-1]
        if isinstance(task, Mapping):
            for key in ("name", "task_name", "env_name", "id"):
                value = task.get(key)
                if isinstance(value, str) and value:
                    return value.rsplit("/", 1)[-1]
        return None

    def contract_manifest(self) -> list[dict[str, Any]]:
        return [
            self._skills[name].contract().to_dict() for name in sorted(self._skills)
        ]

    def contract_manifest_for_task(self, task: Any) -> list[dict[str, Any]]:
        task_name = self.task_name(task)
        names = self._task_skill_map.get(task_name or "", ())
        return [self._skills[name].contract().to_dict() for name in names]

    def hierarchy_graph(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        families = sorted({skill.family for skill in self._skills.values()})
        for name in sorted(self._skills):
            skill = self._skills[name]
            nodes.append(
                {
                    "id": f"atomic:{name}",
                    "level": "atomic",
                    "name": name,
                    "category": skill.family,
                    "contract_schema_version": ATOMIC_SKILL_CONTRACT_SCHEMA_VERSION,
                }
            )
            edges.append(
                {
                    "source": f"atomic:{name}",
                    "target": f"primary-family:{skill.family}",
                    "relation": "member_of",
                }
            )
        for family in families:
            nodes.append(
                {
                    "id": f"primary-family:{family}",
                    "level": "primary_family",
                    "name": family,
                    "representation_only": True,
                }
            )
        family_task_edges: set[tuple[str, str]] = set()
        for task_name, skill_names in sorted(self._task_skill_map.items()):
            nodes.append(
                {
                    "id": f"advanced-task:{task_name}",
                    "level": "advanced_task",
                    "name": task_name,
                    "runtime_binding": "TaskDecomposer",
                }
            )
            for skill_name in skill_names:
                skill = self._skills[skill_name]
                edges.append(
                    {
                        "source": f"atomic:{skill_name}",
                        "target": f"advanced-task:{task_name}",
                        "relation": "required_by",
                    }
                )
                family_task_edges.add((skill.family, task_name))
        edges.extend(
            {
                "source": f"primary-family:{family}",
                "target": f"advanced-task:{task_name}",
                "relation": "supports",
            }
            for family, task_name in sorted(family_task_edges)
        )
        node_ids = {node["id"] for node in nodes}
        if len(node_ids) != len(nodes):
            raise ValueError("atomic hierarchy graph node IDs must be unique")
        if any(
            edge["source"] not in node_ids or edge["target"] not in node_ids
            for edge in edges
        ):
            raise ValueError("atomic hierarchy graph contains a dangling edge")
        return {
            "schema_version": 1,
            "scope": "integrated_composite_seen_representation_graph",
            "claims_boundary": (
                "does_not_claim_final_64_skill_completion_or_causal_effectiveness"
            ),
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "atomic_contract_count": len(self._skills),
                "primary_family_count": len(families),
                "advanced_task_count": len(self._task_skill_map),
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        }

    def manifest_for_task(self, task: Any) -> list[dict[str, Any]]:
        task_name = self.task_name(task)
        names = self._task_skill_map.get(task_name or "", ())
        manifest: list[dict[str, Any]] = []
        for name in names:
            skill = self._skills[name]
            manifest.append(
                {
                    **asdict(skill),
                    "directory_skill_ids": list(TASK_SKILL_DIRECTORY_IDS.get(name, ())),
                    "contract": skill.contract().to_dict(),
                }
            )
        return manifest

    @staticmethod
    def directory_skill_ids_for_name(name: str) -> tuple[str, ...]:
        """Return canonical directory IDs used to join contract knowledge."""

        if not isinstance(name, str) or not name.strip():
            return ()
        return TASK_SKILL_DIRECTORY_IDS.get(name.strip(), ())

    def audit_manifest(self) -> dict[str, Any]:
        return {
            "official_atomic_tasks_reviewed": len(
                ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES
            ),
            "official_atomic_task_names": list(ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES),
            "integrated_task_skills": [
                asdict(self._skills[name]) for name in sorted(self._skills)
            ],
            "composite_seen_task_skill_map": {
                task: list(names)
                for task, names in sorted(self._task_skill_map.items())
            },
            "unified_skill_contracts": self.contract_manifest(),
            "hierarchy_graph": self.hierarchy_graph(),
        }
