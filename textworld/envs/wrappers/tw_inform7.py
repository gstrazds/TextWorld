# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.


# -*- coding: utf-8 -*-
import os
import re

from typing import Mapping, Tuple, List, Optional

import textworld
from textworld.logic import State
from textworld.utils import str2bool
from textworld.utils import check_flag
from textworld.generator.game import Game, GameProgression
from textworld.generator.inform7 import Inform7Game


AVAILABLE_INFORM7_EXTRA_INFOS = ["description", "inventory", "score", "moves"]


class MissingGameInfosError(NameError):
    """
    Thrown if an action requiring GameInfos is used on a game without GameInfos, such as a Frotz game or a
    Glulx game not generated by TextWorld.
    """

    def __init__(self, env):
        msg = ("Can only use '{}' with games generated by "
               " TextWorld. Make sure the generated .json file is in the same "
               " folder as the .ulx or .z8 game file.")
        super().__init__(msg.format(env.__class__.__name__))


def _detect_extra_infos(text: str, tracked_infos: Optional[List[str]] = None) -> Mapping[str, str]:
    """ Detect extra information printed out at every turn.

    Extra information can be enabled via the special command:
    `tw-extra-infos COMMAND`. The extra information is displayed
    between tags that look like this: <COMMAND> ... </COMMAND>.

    Args:
        text: Text outputted by the game.

    Returns:
        A dictionary where the keys are text commands and the corresponding
        values are the extra information displayed between tags.
    """
    tracked_infos = tracked_infos or AVAILABLE_INFORM7_EXTRA_INFOS
    matches = {}
    for tag in tracked_infos:
        if tag not in AVAILABLE_INFORM7_EXTRA_INFOS:
            raise ValueError("TW game doesn't support tag: {}".format(tag))

        regex = re.compile(r"<{tag}>\n(.*)</{tag}>".format(tag=tag), re.DOTALL)
        match = re.search(regex, text)
        if match:
            _, cleaned_text = _detect_i7_events_debug_tags(match.group(1))
            matches[tag] = cleaned_text.strip()
            text = re.sub(regex, "", text)
        else:
            matches[tag] = None

    return matches, text


def _detect_i7_events_debug_tags(text: str) -> Tuple[List[str], str]:
    """ Detect all Inform7 events debug tags.

    In Inform7, debug tags look like this: [looking], [looking - succeeded].

    Args:
        text: Text outputted by the game.

    Returns:
        A tuple containing a list of Inform 7 events that were detected
        in the text, and a cleaned text without Inform 7 debug infos.
    """
    matches = []
    for match in re.findall(r"\[[^]]+\]\n?", text):
        text = text.replace(match, "")  # Remove i7 debug tags.
        tag_name = match.strip()[1:-1]  # Strip starting '[' and trailing ']'.

        if " - succeeded" in tag_name:
            tag_name = tag_name[:tag_name.index(" - succeeded")]
            matches.append(tag_name)

    # If it's got either a '(' or ')' in it, it's a subrule,
    # so it doesn't count.
    matches = [m for m in matches if "(" not in m and ")" not in m]

    return matches, text


class TWInform7(textworld.core.Wrapper):
    """
    Wrapper to play Inform7 games generated by TextWorld.
    """

    def _wrap(self, env):
        super()._wrap(env)
        self._wrapped_env = GameData(self._wrapped_env)
        self._wrapped_env = Inform7Data(self._wrapped_env)
        self._wrapped_env = StateTracking(self._wrapped_env)

    @classmethod
    def compatible(cls, path: str) -> bool:
        """ Check if path point to a TW Inform7 compatible game. """
        basepath, ext = os.path.splitext(path)
        if ext not in [".z8", ".ulx"]:
            return False

        return os.path.isfile(basepath + ".json")

    def copy(self) -> "TWInform7":
        """ Returns a copy this wrapper. """
        env = TWInform7()
        env._wrapped_env = self._wrapped_env.copy()
        return env


class Inform7Data(textworld.core.Wrapper):
    """
    Wrapper that exposes additional information for Inform7 games generated by TextWorld.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tracked_infos = []
        self._prev_state = None

    def _gather_infos(self):
        # Carry over information from previous game step.
        if self._prev_state is not None:
            for attr in self._tracked_infos:
                self.state[attr] = self.state.get(attr) or self._prev_state.get(attr)

        for info in ["score", "moves"]:
            if self.state[info] is not None and type(self.state[info]) is not int:
                self.state[info] = int(self.state[info].strip())

        self.state["won"] = '*** The End ***' in self.state["feedback"]
        self.state["lost"] = '*** You lost! ***' in self.state["feedback"]

    def step(self, command: str):
        self._prev_state = self.state
        self.state, _, _, = self._wrapped_env.step(command)
        extra_infos, self.state["feedback"] = _detect_extra_infos(self.state["feedback"], self._tracked_infos)
        self.state.update(extra_infos)
        self._gather_infos()
        self.state["done"] = self.state["won"] or self.state["lost"]
        return self.state, self.state["score"], self.state["done"]

    def _send(self, command: str) -> str:
        """ Send a command to the game without affecting the Environment's state. """
        return self.unwrapped._send(command)

    def _track_info(self, info):
        extra_infos, _ = _detect_extra_infos(self._send('tw-extra-infos {}'.format(info)))
        self._tracked_infos.append(info)
        self.state.update(extra_infos)

    def reset(self):
        self._tracked_infos = []
        self._prev_state = None
        self.state = self._wrapped_env.reset()

        if self.request_infos.inventory:
            self._track_info("inventory")

        if self.request_infos.description:
            self._track_info("description")

        # Always track moves and score.
        self._track_info("moves")
        self._track_info("score")

        self._gather_infos()
        return self.state

    def copy(self) -> "Inform7Data":
        """ Returns a copy this wrapper. """
        env = Inform7Data()
        env._wrapped_env = self._wrapped_env.copy()
        env._tracked_infos = list(self._tracked_infos)
        env._prev_state = self._prev_state.copy() if self._prev_state is not None else None
        return env


class StateTracking(textworld.core.Wrapper):
    """
    Wrapper that enables state tracking for Inform7 games generated by TextWorld.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._gamefile = None
        self._game = None
        self._inform7 = None
        self._last_action = None
        self._previous_winning_policy = None
        self._current_winning_policy = None
        self._moves = None
        self._game_progression = None

    @property
    def tracking(self):
        return (self.request_infos.intermediate_reward
                or self.request_infos.policy_commands
                or self.request_infos.admissible_commands
                or self.request_infos.facts
                or self.request_infos.last_action)

    def load(self, gamefile: str) -> None:
        self._wrapped_env.load(gamefile)
        self._gamefile = os.path.splitext(gamefile)[0] + ".json"
        try:
            self._game = self._wrapped_env._game
        except AttributeError:
            if not os.path.isfile(self._gamefile):
                raise MissingGameInfosError(self)

            self._game = Game.load(self._gamefile)

        self._game_progression = None
        self._inform7 = Inform7Game(self._game)

    def _gather_infos(self):
        self.state["_game_progression"] = self._game_progression
        self.state["_facts"] = list(self._game_progression.state.facts)

        self.state["won"] = '*** The End ***' in self.state["feedback"]
        self.state["lost"] = '*** You lost! ***' in self.state["feedback"]

        self.state["_winning_policy"] = self._current_winning_policy
        if self.request_infos.policy_commands:
            self.state["policy_commands"] = []
            if self._current_winning_policy is not None:
                self.state["policy_commands"] = self._inform7.gen_commands_from_actions(self._current_winning_policy)

        if self.request_infos.intermediate_reward:
            self.state["intermediate_reward"] = 0
            if self.state["won"]:
                # The last action led to winning the game.
                self.state["intermediate_reward"] = 1

            elif self.state["lost"]:
                # The last action led to losing the game.
                self.state["intermediate_reward"] = -1

            elif self._previous_winning_policy is None:
                self.state["intermediate_reward"] = 0

            else:
                diff = len(self._previous_winning_policy) - len(self._current_winning_policy)
                self.state["intermediate_reward"] = int(diff > 0) - int(diff < 0)  # Sign function.

        if self.request_infos.facts:
            self.state["facts"] = list(map(self._inform7.get_human_readable_fact, self.state["_facts"]))

        self.state["_last_action"] = self._last_action
        if self.request_infos.last_action and self._last_action is not None:
            self.state["last_action"] = self._inform7.get_human_readable_action(self._last_action)

        self.state["_valid_actions"] = self._game_progression.valid_actions
        if self.request_infos.admissible_commands:
            all_valid_commands = self._inform7.gen_commands_from_actions(self._game_progression.valid_actions)
            # To guarantee the order from one execution to another, we sort the commands.
            # Remove any potential duplicate commands (they would lead to the same result anyway).
            self.state["admissible_commands"] = sorted(set(all_valid_commands))

        if self.request_infos.moves:
            self.state["moves"] = self._moves

    def _send(self, command: str) -> str:
        """ Send a command to the game without affecting the Environment's state. """
        return self.unwrapped._send(command)

    def reset(self):
        self.state = self._wrapped_env.reset()
        if not self.tracking:
            return self.state  # State tracking not needed.

        self._send('tw-trace-actions')  # Turn on print for Inform7 action events.
        track_quests = (self.request_infos.intermediate_reward or self.request_infos.policy_commands)
        self._game_progression = GameProgression(self._game, track_quests=track_quests)
        self._last_action = None
        self._previous_winning_policy = None
        self._current_winning_policy = self._game_progression.winning_policy
        self._moves = 0

        self._gather_infos()
        return self.state

    def step(self, command: str):
        self.state, score, done = self._wrapped_env.step(command)
        if not self.tracking:
            return self.state, score, done  # State tracking not needed.

        # Detect what events just happened in the game.
        i7_events, self.state["feedback"] = _detect_i7_events_debug_tags(self.state["feedback"])

        if check_flag("TEXTWORLD_DEBUG"):
            print("[DEBUG] Detected Inform7 events:\n{}\n".format(i7_events))

        self._previous_winning_policy = self._current_winning_policy
        for i7_event in i7_events:
            valid_actions = self._game_progression.valid_actions.copy()
            self._last_action = self._inform7.detect_action(i7_event, valid_actions)
            if self._last_action is None:
                print(valid_actions)
                # try to map command str to an applicable action without using game_progression.valid_actions
                print("DEBUG: StateTracking(without valid_actions) - ATTEMPT TO apply:", command)
                # assert isinstance(self._wrapped_env, Inform7Data)
                self._last_action = self._game_progression.action_if_command_is_applicable(command)

            if self._last_action is not None:
                # An action that affects the state of the game.
                self._game_progression.update(self._last_action)
                self._current_winning_policy = self._game_progression.winning_policy
                self._moves += 1
            else:
                # self.state.feedback = "Invalid command."
                print("DEBUG: StateTracking failed to identify action for command:", command)
                pass  # We assume nothing happened in the game.

        self._gather_infos()
        self.state["done"] = self.state["won"] or self.state["lost"]
        return self.state, score, self.state["done"]

    def copy(self) -> "StateTracking":
        """ Returns a copy this wrapper. """
        env = StateTracking()
        env._wrapped_env = self._wrapped_env.copy()

        env._gamefile = self._gamefile
        env._game = self._game  # Reference
        env._inform7 = self._inform7  # Reference

        env._last_action = self._last_action
        env._moves = self._moves
        if self._previous_winning_policy is not None:
            env._previous_winning_policy = list(self._previous_winning_policy)

        if self._current_winning_policy is not None:
            env._current_winning_policy = list(self._current_winning_policy)

        if self._game_progression is not None:
            env._game_progression = self._game_progression.copy()

        return env


class GameData(textworld.core.Wrapper):
    """
    Wrapper that exposes information contained in the game .json file..
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._gamefile = None
        self._game = None
        self._inform7 = None

    def load(self, gamefile: str) -> None:
        self._gamefile = os.path.splitext(gamefile)[0] + ".json"
        if not os.path.isfile(self._gamefile):
            raise MissingGameInfosError(self)

        try:
            self._game = self._wrapped_env._game
        except AttributeError:
            self._game = Game.load(self._gamefile)
        self._inform7 = Inform7Game(self._game)
        self._wrapped_env.load(gamefile)

    def _gather_infos(self):
        self.state["game"] = self._game
        self.state["command_templates"] = self._game.command_templates
        self.state["verbs"] = self._game.verbs
        self.state["entities"] = self._game.entity_names
        self.state["typed_entities"] = self._game.objects_names_and_types
        self.state["possible_commands"] = self._game.possible_commands
        self.state["possible_admissible_commands"] = self._game.possible_admissible_commands
        self.state["objective"] = self._game.objective
        self.state["max_score"] = self._game.max_score

        for k, v in self._game.metadata.items():
            self.state["extra.{}".format(k)] = v

        def _get_event_facts(event):
            return tuple(map(self._inform7.get_human_readable_fact, event.condition.preconditions))

        if self.request_infos.win_facts:
            self.state["win_facts"] = [[_get_event_facts(e) for e in q.win_events] for q in self._game.quests]

        if self.request_infos.fail_facts:
            self.state["fail_facts"] = [[_get_event_facts(e) for e in q.fail_events] for q in self._game.quests]

    def reset(self):
        self.state = self._wrapped_env.reset()
        self._gather_infos()
        return self.state

    def step(self, command: str):
        self.state, score, done = self._wrapped_env.step(command)
        self._gather_infos()
        return self.state, score, done

    def copy(self) -> "GameData":
        """ Return a soft copy. """
        env = GameData()
        env._wrapped_env = self._wrapped_env.copy()

        env._gamefile = self._gamefile
        env._game = self._game  # Reference
        env._inform7 = self._inform7  # Reference

        return env
