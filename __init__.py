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

from adapt.intent import IntentBuilder
from mycroft.skills.core import MycroftSkill, intent_handler
from mycroft.api import DeviceApi
from mycroft.util.parse import normalize


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


class WinkIoTSkill(MycroftSkill):
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

    # TODO: Move in to MycroftSkill
    def translate_namedvalues(self, name, delim=None):
        """
        Load translation dict containing names and values.

        This loads a simple CSV from the 'dialog' folders.
        The name is the first list item, the value is the
        second.  Lines prefixed with # or // get ignored

        Args:
            name (str): name of the .value file, no extension needed
            delim (char): delimiter character used, default is ','

        Returns:
            dict: name and value dictionary, or [] if load fails
        """
        import csv
        from os.path import join

        delim = delim or ','
        result = {}
        if not name.endswith(".value"):
            name += ".value"

        try:
            with open(join(self.root_dir, 'dialog', self.lang, name)) as f:
                reader = csv.reader(f, delimiter=delim)
                for row in reader:
                    # skip comment lines
                    if not row or row[0].startswith("#"):
                        continue
                    if len(row) != 2:
                        continue

                    result[row[0]] = row[1]

            return result
        except:
            return {}

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

    def set_light(self, lights, state, brightness=1.0):
        self.debug("Setting lights: "+str(state)+"@"+str(brightness), 1, "=")
        if not lights:
            return False

        for light in lights:
            self.debug("Light: "+light["name"]+":"+light["light_bulb_id"], 5)
            body = {"desired_state": {
                       "powered": state,
                       "brightness": brightness
                   }}
            self._winkapi_put("/light_bulbs/"+light["light_bulb_id"],
                              body)
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

    def scale_lights(self, message, scale_by):
        try:
            remainder = self.get_remainder(message)
            room = message.data.get("Room")

            self._device_cache = None  # force update of states
            lights = self.find_lights(remainder, room)
            if lights:
                brightness = lights[0]["last_reading"]["brightness"] * scale_by
                self.set_light(lights, True, brightness)
            else:
                self.speak_dialog("couldnt.find.light")
        except:
            pass

    @intent_handler(IntentBuilder("Brighten").require("Light").require("Brighten").
                    optionally("Room"))
    def handle_brighten_light(self, message):
        self.scale_lights(message, 1.5)

    @intent_handler(IntentBuilder("Dim").require("Light").require("Dim").
                    optionally("Room"))
    def handle_dim_light(self, message):
        self.scale_lights(message, 0.5)

    @intent_handler(IntentBuilder("ChangeLight").require("Light").
                    optionally("Switch").require("OnOff").optionally("Room"))
    def handle_change_light(self, message):
        try:
            on_off = message.data.get("OnOff")
            switch = message.data.get("Switch", "")
            room = message.data.get("Room")
            remainder = self.get_remainder(message)

            lights = self.find_lights(remainder, room)
            if lights:
                # Allow user to say "half", "full", "dim", etc.
                brightness = 1.0
                intensities = self.translate_namedvalues("Intensities")
                for i in intensities:
                    if contains_word(message.data.get("utterance"), i):
                        self.debug("Match intensity: "+i)
                        brightness = intensities[i]

                self.set_light(lights, on_off != "off", brightness)
            else:
                self.speak_dialog("couldnt.find.light")
        except:
            pass

    # Disabling for now.  "What is your name" is triggering this intent
    # Might need Padatious to handle this?
    #
    # @intent_handler(IntentBuilder("").optionally("Light").
    #                 require("Query").optionally("Room"))
    def handle_query_light(self, message):
        try:
            remainder = self.get_remainder(message)

            self._device_cache = None  # force update of states
            lights = self.find_lights(remainder,
                                      message.data.get("Room"))
            if lights:
                # Just give the value of the first light
                if (not lights[0]["last_reading"]["powered"] or
                        lights[0]["last_reading"]["brightness"] < 0.001):
                    state = self.translate("off")
                elif lights[0]["last_reading"]["brightness"] < 0.75:
                    state = self.translate("dimmed")
                else:
                    state = self.translate("on")

                self.speak_dialog("light.level.is", data={"state": state})
            else:
                self.speak_dialog("couldnt.find.light")
        except:
            pass

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
