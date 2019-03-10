# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import requests
import json
import datetime

from mycroft.api import DeviceApi
from mycroft.util.parse import normalize
from mycroft.skills.common_iot_skill import CommonIoTSkill
from mycroft.skills.common_iot_skill import IoTRequest, Action, Attribute, Thing
from mycroft.util.log import getLogger


LOG = getLogger(__name__)


# TODO: Move the mycroft.util.parse
def contains_word(sentence, word):
    import re
    words = word if isinstance(word, list) else [word]
    for w in words:
        if not w:
            continue
        res = re.search(r"\b" + re.escape(w) + r"\b", sentence)
        if res:
            return res
    return False


# TODO: Move the mycroft.util.parse
def fuzzy_match(x, against):
    from difflib import SequenceMatcher
    # Returns a value 0.0 - 1.0
    return SequenceMatcher(None, x, against).ratio()


# TODO: Move to mycroft.util.time
def to_timestamp(dt_utc):
    # return epoch (milliseconds since 1970-1-1) as float
    return (dt_utc - datetime.datetime(1970, 1, 1)).total_seconds()


# TODO: Move to mycroft.util.time
def from_timestamp(epoch_utc):
    # return a UTC datatime from an epoch millisecond count
    epoch_origin = datetime.datetime(1970, 1, 1)
    return epoch_origin + datetime.timedelta(0, epoch_utc)

# For now, using the Wink V1 API which won't require anything except the
# username and password.  For details, see:
# https://wink.docs.apiary.io/#reference

# These normally shouldn't be published, but Quirky released these
# accidentally years ago and have left them alive.  Use these until
# we switch to the Quirky v2 API
#
client_id = 'quirky_wink_ios_app'
client_secret = 'ce609edf5e85015d393e7859a38056fe'


class WinkIoTSkill(CommonIoTSkill):
    debug_level = 3  # 0-10, 10 showing the most debugging info

    def __init__(self):
        super(WinkIoTSkill, self).__init__(name="WinkIoTSkill")
        self.settings["access_token"] = None
        self.settings["refresh_token"] = None
        self.settings["token_expiration"] = to_timestamp(
                                                datetime.datetime.utcnow())
        self.settings['email'] = ""
        self.settings['password'] = ""
        self._device_cache = None
        self._group_cache = None
        self._active_room = None
        self._entities = dict()
        self._intensities = dict()

    def initialize(self):
        self._initialize_entities()
        self._intensities = self.translate_namedvalues("Intensities")
        self.register_entities_and_scenes()

    def get_entities(self):
        return self._entities.keys()

    def get_scenes(self):
        LOG.info("intensities: " + str(self._intensities))
        return self._intensities.keys()

    def _initialize_entities(self):
        groups = self.wink_groups.get("data")
        devices = self.wink_devices.get("data")
        LOG.info("Wink group data = " + str(groups))
        LOG.info("Wink dev data = " + str(devices))
        LOG.info("Raw group data = " + str(self.wink_groups))
        LOG.info("Raw dev data = " + str(self.wink_devices))

        groups = {group["name"]: [member["object_id"]
                  for member in group["members"]
                      if member["object_type"] == "light_bulb"]
                  for group in groups}
        lights = {dev["name"]: [dev["object_id"]]
                  for dev in devices if self._is_light(dev)}
        lights.update(groups)  # Groups take precedence
        LOG.info("entities are " + str(lights))
        self._entities = lights

    def debug(self, message, level=1, char=None):
        # Debugging assistance.
        # Lower number are More important.  Only number less than the
        # debug_level get logged.  Indentation is the inverse of this,
        # with level 0 having 10 dashes as a prefix, 1 have 9, etc.
        if level > WinkIoTSkill.debug_level:
            return

        if not char:
            char = "-"
        self.log.debug(char*(10-level) + " " + message)

    def get_remainder(self, message):
        # Remove words "consumed" by the intent match, e.g. if they
        # say 'Turn on the family room light' and there are entity
        # matches for "turn on" and "light", then it will leave
        # behind "the family room", which we normalize to "family room"
        utt = message.data["utterance"]
        for token in message.data["__tags__"]:
            utt = utt.replace(token["key"], "")
        return normalize(utt)

    @property
    def room_name(self):
        # Assume the "name" of the device is the "room name"
        device = DeviceApi().get()
        return device["name"]

    def _winkapi_auth(self):
        now = datetime.datetime.utcnow()

        # Check if access token exists and hasn't expired
        if (self.settings["access_token"] and
                to_timestamp(now) < self.settings["token_expiration"]):
            # Already logged in
            return True

        if not self.settings['email'] or not self.settings['password']:
            self.speak_dialog("need.to.configure")
            return False

        # Attempt to authorize with the users ID and password
        body = {
            "client_id": client_id,
            "client_secret": client_secret,
            "username": self.settings['email'],
            "password": self.settings['password'],
            "grant_type": "password"
        }

        # Post
        res = requests.post("https://api.wink.com/oauth2/token", body)
        if res.status_code == requests.codes.ok:
            # Save the token for ongoing use
            data = json.loads(res.text)
            self.settings["access_token"] = data["access_token"]
            self.settings["refresh_token"] = data["refresh_token"]

            exp_secs = data["expires_in"]  # seconds until expires
            expiration = now + datetime.timedelta(0, exp_secs)

            self.settings["token_expiration"] = to_timestamp(expiration)
            return True
        else:
            # Notify the user then exit completely (nothing else can
            # be done if not registered)
            self.speak_dialog("unable.to.login")
            raise Exception('unable.to.login')

    def _winkapi_get(self, path):
        if not self._winkapi_auth():
            self.log.error("Failed to login")
            return False

        headers = {'Authorization': 'Bearer ' + self.settings["access_token"]}
        res = requests.get("https://winkapi.quirky.com" + path,
                           headers=headers)
        if res.status_code == requests.codes.ok:
            # success
            self.debug("Read devices!", 9, char="*")
            return res.json()
        else:
            self.debug("Failed to read devices!", char="!")
            self.debug(res.status_code, 5)
            self.debug(res.text, 5)
            return None

    def _winkapi_put(self, path, body):
        if not self._winkapi_auth():
            self.log.error("Failed to login")
            return False

        headers = {'Authorization': 'Bearer ' + self.settings["access_token"],
                   'Content-Type': 'application/json'}
        res = requests.put("https://winkapi.quirky.com" + path,
                           headers=headers, data=json.dumps(body))

        if res.status_code == requests.codes.ok:
            # success
            self.debug("Successful PUT to device!", 2)
            return res.json()
        else:
            self.debug("Failed to PUT devices", char="!")
            self.debug("URL: "+"https://winkapi.quirky.com"+path, 5)
            self.debug(res.status_code, 5)
            self.debug(res.text, 5)
            return None

    @property
    def wink_devices(self):
        # Retrieve a list of devices associated with the account
        if not self._device_cache:
            self._device_cache = self._winkapi_get("/users/me/wink_devices")
        return self._device_cache

    @property
    def wink_groups(self):
        # Retrieve a list of groups of devices associated with the account
        if not self._group_cache:
            self._group_cache = self._winkapi_get("/users/me/groups")
        return self._group_cache

    def _is_light(self, dev):
        return "light_buld_id" in dev

    def get_lights(self, search_name):
        if not search_name:
            return None
        name = normalize(search_name)
        self.debug("Searching for: "+name, 2, char="=")

        # First fuzzy search the groups
        best = None
        best_score = 0
        if self.wink_groups:
            for group in self.wink_groups["data"]:
                groupname = normalize(group["name"])
                score = fuzzy_match(groupname, name)
                self.debug(groupname + " : " + str(score), 5)
                if score > 0.6 and score > best_score:
                    best_score = score
                    best = group

        if not self.wink_devices:
            # can't even return group matches without device info
            return None

        best_group_score = best_score
        group_lights = []
        group_IDs = []
        if best:
            # Collect the light IDs from the group that was found
            for member in best["members"]:
                if member["object_type"] == "light_bulb":
                    group_IDs.append(member["object_id"])

        best = None
        for dev in self.wink_devices["data"]:
            if "light_bulb_id" in dev:   # check if light_bulb

                # Gather group lights (just in case the group wins)
                if dev["light_bulb_id"] in group_IDs:
                    group_lights.append(dev)

                # score the bulb name match
                lightname = normalize(dev["name"])
                score = fuzzy_match(lightname, name)
                self.debug(lightname + " : " + str(score), 5)
                if score > best_score:
                    best_score = score
                    best = dev

        if group_lights and best_group_score >= best_score:
            self.debug("Group wins", 3, char="*")
            return group_lights
        elif best and best_score > 0.6:
            return [best]

        return None

    def set_light(self, entity: str, action: Action, brightness=1.0):
        lights = (self._entities.get(entity) or
                  self._entities.get(self.room_name))
        powered = action == Action.ON
        self.debug("Setting lights: " + str(powered) +
                   "@" + str(brightness), 1, "=")
        if not lights:
            return False

        for light in lights:
            body = {"desired_state": {
                       "powered": powered,
                       "brightness": brightness
                   }}
            self._winkapi_put("/light_bulbs/" + light, body)
        return True

    def find_lights(self, remainder, room):
        if room:
            remainder = None

        # First, see if the user specified a room ("turn on the light in
        # the kitchen") or just mentioned it in the request ("turn on the
        # kitchen light")
        lights = self.get_lights(room)
        if lights:
            self._active_room = room
        else:
            lights = self.get_lights(remainder)
            if lights:
                self._active_room = remainder

        # If no location specified, default to using the device name as
        # a room name...
        if not lights and not room:
            lights = self.get_lights(self.room_name)
            self._active_room = self.room_name

        return lights

    def scale_lights(self, requst: IoTRequest, scale_by: float):
        try:
            room = requst.entity or self.room_name

            self._device_cache = None  # force update of states TODO what does this do?
            lights = self._entities[room]
            if lights:
                brightness = lights[0]["last_reading"]["brightness"] * scale_by
                self.set_light(lights, True, brightness)
            else:
                self.speak_dialog("couldnt.find.light")
        except:
            pass

    def can_handle(self, request: IoTRequest):
        if not (request.thing == Thing.LIGHT
                or request.entity in self._entities):
            return False, None
        if request.scene and request.scene not in self._intensities:
            return False, None
        if request.attribute and not request.attribute == Attribute.BRIGHTNESS:
            return False, None
        if request.action in (Action.ON, Action.OFF):
            return True, None
        elif request.action in (Action.INCREASE, Action.DECREASE):
            return True, None
        return False, None

    def run_request(self, request: IoTRequest, callback_data: dict):
        if request.action in (Action.ON, Action.OFF):
            intensity = 1.0
            if request.scene:
                intensity = self._intensities[request.scene]
            self.set_light(request.entity, request.action, intensity)
        if request.action == Action.INCREASE:
            self.scale_lights(request, 1.5)
        if request.action == Action.DECREASE:
            self.scale_lights(request, 0.5)



    # @intent_handler(IntentBuilder("Brighten").require("Light").require("Brighten").
    #                 optionally("Room"))
    # def handle_brighten_light(self, message):
    #     self.scale_lights(message, 1.5)
    #
    # @intent_handler(IntentBuilder("Dim").require("Light").require("Dim").
    #                 optionally("Room"))
    # def handle_dim_light(self, message):
    #     self.scale_lights(message, 0.5)
    #
    # @intent_handler(IntentBuilder("ChangeLight").require("Light").
    #                 optionally("Switch").require("OnOff").optionally("Room"))
    # def handle_change_light(self, message):
    #     try:
    #         on_off = message.data.get("OnOff")
    #         switch = message.data.get("Switch", "")
    #         room = message.data.get("Room")
    #         remainder = self.get_remainder(message)
    #
    #         lights = self.find_lights(remainder, room)
    #         if lights:
    #             # Allow user to say "half", "full", "dim", etc.
    #             brightness = 1.0
    #             intensities = self.translate_namedvalues("Intensities")
    #             for i in intensities:
    #                 if contains_word(message.data.get("utterance"), i):
    #                     self.debug("Match intensity: "+i)
    #                     brightness = intensities[i]
    #
    #             self.set_light(lights, on_off != "off", brightness)
    #         else:
    #             self.speak_dialog("couldnt.find.light")
    #     except:
    #         pass

    def converse(self, utterances, lang='en-us'):
        if self._active_room:
            self._device_cache = None  # force update of states
            lights = self.get_lights(self._active_room)
            if contains_word(utterances[0], self.translate_list("brighter")):
                self.debug("Conversational brighting")
                brightness = lights[0]["last_reading"]["brightness"] * 1.5
                return self.set_light(lights, True, brightness)
            elif contains_word(utterances[0], self.translate_list("dimmer")):
                self.debug("Conversational dimming")
                brightness = lights[0]["last_reading"]["brightness"] * 0.5
                return self.set_light(lights, True, brightness)
        return False

def create_skill():
    return WinkIoTSkill()
