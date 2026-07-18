import os, sys
import json
import cv2
import argparse
from typing import List, Tuple, Dict
import ai2thor.controller
from ai2thor.platform import CloudRendering
import openai

# save a json file with your openai api key in your
# home folder as {"my_openai_api_key": "INSERT API HERE"}
with open(os.path.expanduser("~") + "/openai_key.json") as json_file:
    key = json.load(json_file)
    openai_api_key = key["my_openai_api_key"]
openai.api_key = openai_api_key
os.environ["OPENAI_API_KEY"] = openai_api_key
# TODO(G2): remove implicit openai_key.json side effect

from AI2Thor.base_env import convert_dict_to_string, BaseEnv
from AI2Thor.Tasks.task_mapper import get_closest_task
from AI2Thor.Tasks.get_scene_init import get_scene_initializer

AGENT_NAMES=["Alice", "Bob", "Charlie", "David", "Emma"]

class AI2ThorEnv(BaseEnv):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self.scene = args.scene
        self.num_agents = args.num_agents
        self.controller = ai2thor.controller.Controller(
            width=1000,
            height=1000,
            scene=args.scene,
            gridSize=0.25,
        )
        self.agent_names = AGENT_NAMES
        self.move_actions = ["MoveAhead", "MoveBack", "MoveRight", "MoveLeft"]
        self.rotate_actions = ["RotateRight", "RotateLeft"]
        self.obs_summary_llm_cache_path = "AI2Thor/summary_llm_cache.pkl"
        # self.model = "gpt-4-0314"
        self.model = args.model
        self.use_obs_summariser = (
            args.use_obs_summariser if hasattr(args, "use_obs_summariser") else False
        )
        self.use_act_summariser = (
            args.use_act_summariser if hasattr(args, "use_act_summariser") else False
        )
        self.use_action_failure = (
            args.use_action_failure if hasattr(args, "use_action_failure") else False
        )
        self.use_shared_subtask = (
            args.use_shared_subtask if hasattr(args, "use_shared_subtask") else False
        )
        self.use_separate_subtask = (
            args.use_separate_subtask
            if hasattr(args, "use_separate_subtask")
            else False
        )
        self.use_separate_memory = (
            args.use_separate_memory if hasattr(args, "use_separate_memory") else False
        )
        self.use_shared_memory = (
            args.use_shared_memory if hasattr(args, "use_shared_memory") else False
        )
        self.use_plan = args.use_plan if hasattr(args, "use_plan") else False
        self.forceAction = args.forceAction
        self.previous_actions = [None] * self.num_agents  # to store previous actions
        self.previous_success = [None] * self.num_agents  # to store previous success
        self.inventory = [
            "nothing"
        ] * self.num_agents  # to store what objects the agent is holding
        self.output_format = {
            "Alice's action": "<Alice's action>",
            "Bob's action": "<Bob's action>",
        }
        # to store previous observations
        self.all_obs = []
        self.agent_failure_acts = {
            self.agent_names[i]: [] for i in range(self.num_agents)
        }
        if self.use_shared_subtask:
            self.subtasks = ["We still need to decide on a subtasks"]
        elif self.use_separate_subtask:
            self.subtasks = ["I still need to decide on a subtask"] * self.num_agents

        if self.use_separate_memory:
            self.memory = [
                "Memory is empty because I have not taken an action yet"
            ] * self.num_agents
        elif self.use_shared_memory:
            self.memory = ["Memory is empty because no actions taken yet"]

        if self.use_plan:
            self.open_subtasks = "None"
            self.closed_subtasks = "None"

        # overhead vs agent-centric
        self.overhead=False
        self.verbose=True

        self.skip_save_dir=False

    # for exploration purposes
    @property
    def curr_step_success(self):
        names=list(self.action_success_history.keys())
        if len(self.action_success_history[names[0]])==0:
            return None
        successes = [self.action_success_history[name][-1] for name in names]
        return successes

    def reset(
        self,
        task: str = "bring a tomato, lettuce and bread to the countertop to make a sandwich",
        ignore_applicable : bool = False,
    ):
        # ignore_applicable -> set to true iff doing reset for pre-initializations

        """
        input_dict = {
            Task: <task>,
            Alice's observation: <observation>,
            Alice's state: <state>,
            Alice's feasible actions: <feasible actions>,
            Alice's previous observation: <previous observation>,
            Alice's previous action: <previous action>,
            Bob's observation: <observation>,
            Bob's state: <state>,
            Bob's feasible actions: <feasible actions>,
            Bob's previous observation: <previous observation>,
            Bob's previous action: <previous action>,
        }
        """

        self.visibilityDistance=1.5 # set this so we can use it in navigation later on
        self.event = self.controller.step(
            dict(
                action="Initialize",
                gridSize=0.25,
                renderObjectImage=True,
                agentCount=self.num_agents,
                visibilityDistance=self.visibilityDistance
            )
        )
        self.object_dict = {}
        self.task = task
        self.input_dict = {}
        self.input_dict["Task"] = task
        self.task, self.closest_task_instr = get_closest_task(task)

        if self.verbose:
            print("_" * 50)
            print(f"Pre-initializing environment with closest task: {self.task}")
            print("_" * 50)

        if not self.skip_save_dir:
            self.create_save_dirs()

        self.pre_sum_obs_list = {
            self.agent_names[i]: [] for i in range(self.num_agents)
        }

        # move agent-0 to an initial position away from agent-1
        self.agent_init_pos(0)
        self.step_num = [0] * self.num_agents
        self.all_obs = []

        # @UROPs this is where preinit will be called
        scene_initializer, checker = get_scene_initializer(self.task, self.scene, ignore_applicable)

        if scene_initializer is not None:
            if self.verbose:
                print("_" * 50)
                print("Preinitializing the environment")
                print("_" * 50)

            self.checker = checker.Checker()
            self.event = scene_initializer.SceneInitializer().preinit(
                self.event, self.controller
            )

            # give information about all the object ids to checker (it needs information on the current floorplan to see amt of objs for each name)
            all_oids = list(map(lambda o : o['objectId'], self.controller.last_event.metadata["objects"]))
            self.checker.all_objects(obj_ids=all_oids, scene=self.scene)

        if self.verbose:
            print("_" * 50)
            print("Subtasks to complete:")
            print("\n".join(self.checker.subtasks))


        #######################################
        # make a dict of lists for action history for each agent
        self.action_history = {self.agent_names[i]: [] for i in range(self.num_agents)}
        self.action_success_history = {
            self.agent_names[i]: [] for i in range(self.num_agents)
        }
        self.step_nums_history = {
            self.agent_names[i]: [] for i in range(self.num_agents)
        }
        self.all_obs_dict = {self.agent_names[i]: [] for i in range(self.num_agents)}

        for agent_id in range(self.num_agents):
            if not self.skip_save_dir:
                self.save_frame(agent_id)
            agent_name = self.agent_names[agent_id]
            obs, obs_list = self.generate_obs_text(agent_id, prefix="")
            self.input_dict[agent_name + "'s observation"] = obs
            state = self.get_agent_state(agent_id)
            self.input_dict[agent_name + "'s state"] = state
            if self.use_act_summariser:
                self.input_dict[
                    agent_name + "'s feasible actions"
                ] = self.get_feasible_actions(obs_list, agent_id)
            # obs_list = self.convert_str2list(obs)
            self.input_dict[agent_name + "'s previous observation"] = "None"
            self.input_dict[
                agent_name + "'s previous action"
            ] = "I have not taken any action yet"
            if self.use_action_failure:
                self.input_dict[agent_name + "'s previous failures"] = "None"
            self.all_obs.append(obs_list)
            self.all_obs_dict[agent_name] = obs_list
            if self.use_shared_subtask:
                self.input_dict["Robots' subtasks"] = self.subtasks[0]
            elif self.use_separate_subtask:
                self.input_dict[agent_name + "'s subtasks"] = self.subtasks[agent_id]
            if self.use_separate_memory:
                self.input_dict[agent_name + "'s memory"] = self.memory[agent_id]
            elif self.use_shared_memory:
                self.input_dict["Robots' combined memory"] = self.memory[0]
            if self.use_plan:
                self.input_dict["Robots' open subtasks"] = self.open_subtasks
                self.input_dict["Robots' completed subtasks"] = self.closed_subtasks

        return convert_dict_to_string(self.input_dict)

    def update_subtask(self, subtask: str, agent_id: int):
        """
        Update the subtask for the agent
        """
        self.subtasks[agent_id] = subtask

    def update_memory(self, memory: str, agent_id: int):
        """
        Update the memory for the agent
        """
        self.memory[agent_id] = memory

    def update_plan(self, plan: str):
        """
        Update the plan for the agent
        """
        self.plan = plan

    def update_current_state(self, act_texts: List[str]):
        """
        Update the input dictionary with the current state of the environment
        """
        self.new_all_obs = []  # to store previous observations
        for agent_id in range(self.num_agents):
            agent_name = self.agent_names[agent_id]
            obs, obs_list = self.generate_obs_text(agent_id, prefix="")
            self.input_dict[agent_name + "'s observation"] = obs
            state = self.get_agent_state(agent_id)
            self.input_dict[agent_name + "'s state"] = state
            if self.use_act_summariser:
                self.input_dict[
                    agent_name + "'s feasible actions"
                ] = self.get_feasible_actions(obs_list, agent_id)
            # obs_list = self.convert_str2list(obs)
            self.new_all_obs.append(obs_list)
            self.input_dict[agent_name + "'s previous observation"] = self.all_obs[
                agent_id
            ]
            self.input_dict[agent_name + "'s previous action"] = act_texts[agent_id]
            if self.use_action_failure:
                self.input_dict[
                    agent_name + "'s previous failures"
                ] = self.get_act_failure_text(
                    self.agent_failure_acts[agent_name], agent_id
                )
            if self.use_shared_subtask:
                self.input_dict["Robots' subtasks"] = self.subtasks[0]
            elif self.use_separate_subtask:
                self.input_dict[agent_name + "'s subtask"] = self.subtasks[agent_id]

            if self.use_separate_memory:
                self.input_dict[agent_name + "'s memory"] = self.memory[agent_id]
            elif self.use_shared_memory:
                self.input_dict["Robots' combined memory"] = self.memory[0]
            if self.use_plan:
                self.input_dict["Robots' open subtasks"] = self.open_subtasks
                self.input_dict["Robots' completed subtasks"] = self.closed_subtasks
            # if self.use_memory:
            #     self.input_dict[agent_name + "'s memory"] = self.memory[agent_id]
        self.all_obs = self.new_all_obs

    def get_planner_llm_input(self):
        """
        Returns the input to the subtask LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[0]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[1]}'s observation: list of objects the {agent_name[1]} is observing,
        Robots' open subtasks: list of subtasks the robots are supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        }}

        """
        # extract the agent_name's observations based on how many agents there are
        planner_llm__input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            planner_llm__input_feats.append(agent_name + "'s observation")
        planner_llm__input_feats.extend(
            ["Robots' open subtasks", "Robots' completed subtasks"]
        )
        return dict((k, self.input_dict[k]) for k in planner_llm__input_feats)

    def get_verifier_llm_input(self):
        """
        Returns the input to the verifier LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[i]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[i]}'s previous action: previous action of the {agent_name[0]},
        Robots' open subtasks: list of subtasks the robots are supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        Robots' combined memory: description of robots' combined memory}}

        """
        # extract the agent_name's observations based on how many agents there are
        verifier_llm_input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            verifier_llm_input_feats.extend(
                [
                    agent_name + "'s observation",
                    agent_name + "'s state",
                    agent_name + "'s previous action",
                ]
            )
        verifier_llm_input_feats.extend(
            [
                "Robots' open subtasks",
                "Robots' completed subtasks",
                "Robots' combined memory",
            ]
        )
        return dict((k, self.input_dict[k]) for k in verifier_llm_input_feats)

    def get_action_llm_input(self, failure_module=False):
        """
        Returns the input to the subtask LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[i]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[i]}'s state: description of {agent_name[0]}'s state,
        {agent_name[i]}'s previous action: description of what {agent_name[0]} did in the previous time step and whether it was successful,
        {agent_name[i]}'s previous failures: if {agent_name[0]}'s few previous actions failed, description of what failed,
        Robots' open subtasks: list of subtasks  supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        Robots' subtask: description of the subtasks the robots were trying to complete in the previous step,
        Robots' combined memory: description of robot's combined memory}}
        """
        # extract the agent_name's observations based on how many agents there are
        # current additional info from failure ["failure reason"]
        llm_input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            llm_input_feats.extend(
                [
                    agent_name + "'s observation",
                    agent_name + "'s state",
                    agent_name + "'s previous action",
                    agent_name + "'s previous failures",
                ]
            )
        llm_input_feats.extend(
            [
                "Robots' open subtasks",
                "Robots' completed subtasks",
                # "Robots' subtasks",
                "Robots' combined memory",
            ]
        )

        if failure_module:
            # post action / failure module inputs
            # v0 - give failure reason (override environment-given), add logic for next action
            llm_input_feats.extend(
                    [
                        "failure reason",
                        # "logic for next action",
                    ]
            )
        return dict((k, self.input_dict[k]) for k in llm_input_feats)

    def get_failure_llm_input(self):
        """
        Returns the input to the subtask LLM
        Same input as action module currently, but at time t+1.

        ### INPUT FORMAT ###

        {{Task: description of the task the robots are supposed to do,
        {agent_name[i]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[i]}'s state: description of {agent_name[0]}'s state,
        {agent_name[i]}'s previous action: description of what {agent_name[0]} did in the previous time step and whether it was successful,
        {agent_name[i]}'s previous failures: if {agent_name[0]}'s few previous actions failed, description of what failed,
        Robots' open subtasks: list of subtasks  supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        Robots' subtask: description of the subtasks the robots were trying to complete in the previous step,
        Robots' combined memory: description of robot's combined memory}}
        """
        # extract the agent_name's observations based on how many agents there are
        llm_input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            llm_input_feats.extend(
                [
                    agent_name + "'s observation",
                    agent_name + "'s state",
                    agent_name + "'s previous action",
                    agent_name + "'s previous failures",
                ]
            )
        llm_input_feats.extend(
            [
                "Robots' open subtasks",
                "Robots' completed subtasks",
                # "Robots' subtasks",
                "Robots' combined memory",
            ]
        )

        return dict((k, self.input_dict[k]) for k in llm_input_feats)

    def step(self, actions: List):
        """
        Execute the actions for all the agents in the environment
        Return the observation for all the agents
        """
        act_successes, act_texts = [], []
        for agent_id in range(self.num_agents):
            error_type=None
            if "NavigateTo" in actions[agent_id]:
                act_success, error_type = self.navigation_step(
                    actions[agent_id], agent_id
                )
            elif actions[agent_id] in ["Done", "Idle"]:
                act_success = True
                # TODO: save frame here too (as it's useful for scene reconstruction later on)

            # @ coela - added this to handle SendMessage
            elif "SendMessage" in actions[agent_id]:
                act_success = True
            else:
                # TODO: add previous action check here; check if action was taken in the previous step
                action_dict = self.parse_action(actions[agent_id], agent_id)

                self.event = self.controller.step(action_dict)
                act_success = self.event.events[agent_id].metadata["lastActionSuccess"]
                self.previous_success[agent_id] = act_success
                self.step_num[agent_id] += 1
                self.save_frame(agent_id)

            # if action is successful, make the agent_failure_acts list empty
            if act_success:
                self.agent_failure_acts[self.agent_names[agent_id]] = []
                if "PickupObject" in actions[agent_id]:
                    self.inventory[agent_id] = self.get_agent_object_held(agent_id)
            else:
                self.agent_failure_acts[self.agent_names[agent_id]].append(
                    actions[agent_id]
                )

            # add the action and succes to the action and success history
            self.action_history[self.agent_names[agent_id]].append(actions[agent_id])
            self.action_success_history[self.agent_names[agent_id]].append(act_success)
            self.step_nums_history[self.agent_names[agent_id]].append(
                self.step_num[agent_id]
            )

            # NOTE that self.inventory[agent_id] is updated for PutObject in self.get_act_text
            # this means that the inventory is not updated after the PutObject action
            # is executed after the action is successful, so at this point, the inventory
            # is still the same as it was before the action was executed
            self.checker.perform_metric_check(
                actions[agent_id], act_success, self.inventory[agent_id]
            )

            #if self.verbose:
            #   print(f"Completed Subtasks: ")
            #   print("\n".join(self.checker.subtasks_completed))

            act_text = self.get_act_text(
                actions[agent_id], act_success, agent_id, error_type=error_type
            )
            act_texts.append(act_text)
            act_successes.append(act_success)

        self.update_current_state(act_texts)
        return convert_dict_to_string(self.input_dict), act_successes

    def navigation_step(self, action: str, agent_id: int):
        """
        Execute all the navigation actions for the agent in the environment
        Return the observation for all the agents
        action: "NavigateTo(CounterTop_1)"
        """
        self.get_object_in_view(self.event, agent_id)
        object_id = action.split("(")[1].split(")")[0]
        error_type=None
        try:
            actions = self.get_navigation_actions(agent_id, object_id)
        except (KeyError, ValueError) as k:
            # assuming valueerror is from trying to find number in array (if object x_1 exists but not x_77)
            return False, "key" 

        if actions is None:
            # nonetype for actions means that object doesn't exist for the agent (not visible, so not navigatable)
            return False, "no-actions"


        act_successes = []
        for i, action in enumerate(actions):
            action_dict = self.parse_action(action, agent_id)
            # TODO save frames here
            self.event = self.controller.step(action_dict)
            act_success = self.event.events[agent_id].metadata["lastActionSuccess"]
            self.step_num[agent_id] += 1
            # TODO
            self.save_frame(agent_id)

            # NOTE: Sometimes the LookDown/LookUp action is not successful
            # because we can only move it by 30 degrees in get_shortest_path,
            # but the agent is still able to see the object
            if "Look" in action:
                # print("Look action sucess: ", act_success)
                act_success = True

            # don't break loop if first action (teleport) is failure (it just means it's at correct position)
            if not act_success and i != 0:
                break
            # make sure that failures in alignment aren't counted as actions failures (they aren't, just means the agent is already aligned)
            if not "align" in action.lower():
                act_successes.append(act_success)


        # effective failure if not close enough to object (can't interact)
        object_id=self.convert_readable_object_to_id(object_id)
        # this should be restricted to interactable (not it's interactable objects once we act to account for navigateto non-interactable)
        obj_interactable=object_id in self.get_object_in_view(self.controller.last_event, agent_id)
        # interactability error
        error_type=None if obj_interactable else "non-interactable"

        # print(actions, act_successes)
        # return true only if all the navigation actions are successful
        return all(act_successes) and obj_interactable, error_type

    # Util functions for overhead images & frame capture
    def set_overhead(self, o):
        self.overhead=o

    # get overhead image
    def _get_ceiling_image(self):
        # puts the camera in the ceiling, then puts it back with the robot
        event = self.controller.step('ToggleMapView')
        # toggle back for later
        self.controller.step('ToggleMapView')
        return event.cv2img

    def _write_image(self, pth, img):
        cv2.imwrite(str(pth), img)

    def _create_path(self, pth):
        if not os.path.exists(pth):
            os.makedirs(pth)

    def save_frame(self, agent_id: int):
        agent_dir_pov = self.render_image_path / self.agent_names[agent_id] / 'pov'
        agent_dir_overhead = self.render_image_path / self.agent_names[agent_id] / 'overhead'
        self._create_path(agent_dir_pov)
        self._create_path(agent_dir_overhead)

        # add overhead view for gif
        if self.overhead:
            img = self._get_ceiling_image()
            pth = agent_dir_overhead / f"frame_{self.step_num[agent_id]}.png"
            self._write_image(pth, img)

        img = self.event.events[agent_id].cv2img
        pth = agent_dir_pov / f"frame_{self.step_num[agent_id]}.png"
        self._write_image(pth, img)

    def get_frame(self, agent_id: int):
        image_path = (
            self.render_image_path
            / self.agent_names[agent_id]
            / 'pov'
            / f"frame_{self.step_num[agent_id]}.png"
        )
        return image_path

    def generate_obs_text(
        self, agent_id: int, prefix="I see: "
    ) -> Tuple[str, List[str]]:
        """
        Returns a string that describes the agent's observation
        """
        obs_text = ""
        obs_text += prefix
        objects_in_view = self.get_object_in_view(self.event, agent_id)
        # ['Cabinet_1', 'UpperCabinets_1', 'Egg_1', 'CounterTop_1']
        obs_list = self.get_readable_object_list(objects_in_view)
        self.pre_sum_obs_list[self.agent_names[agent_id]] = obs_list
        if self.use_obs_summariser:
            obs_list = self.summarise_obs(obs_list)
        obs_text += str(obs_list)
        return obs_text, obs_list

    def get_task_success(self):
        """
        Returns True if the task is done, else False
        """
        pass


if __name__ == "__main__":
    from AI2Thor.env_new import AI2ThorEnv

    class Config:
        def __init__(self):
            self.num_agents = 2
            self.scene = "FloorPlan1"
            self.scene_name = "FloorPlan1"
            self.model = "gpt-4"
            self.use_obs_summariser = False
            self.use_act_summariser = False
            self.use_action_failure = True
            self.use_shared_subtask = True
            self.use_separate_subtask = False
            self.use_future_message = True
            self.forceAction = False
            self.use_memory = True
            self.use_plan = True
            self.use_separate_memory = False
            self.use_shared_memory = True
            self.temperature = 0.7

    config = Config()

    env = AI2ThorEnv(config)
    d = env.reset()
    actions = []
    for i in range(20):
        print(f"Step {i}")
        action = env.get_gpt_response()
        actions.append(action)
        print(action)
        env.step(action)
