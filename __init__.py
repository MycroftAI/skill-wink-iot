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
from mycroft.skills.common_iot_skill import CommonIoTSkill
from mycroft.skills.common_iot_skill import IoTRequest, Action,\
    Attribute, Thing
from mycroft.util.log import getLogger
from typing import Any, Callable


LOG = getLogger(__name__)


def cache(func: Callable) -> Callable:
    """
    Decorator to cache a function's result.

    This sets a property of the function itself
    to the result of the function call. This avoids
    the need to keep state in another object.

    This adds a `use_cache` parameter to the function.
    If set to False, the result will be regenerated. It
    is True by default.
    """
    func.cached_result = None

    def new_func(self, use_cache: bool = True) -> Any:
        if not use_cache or not func.cached_result:
            func.cached_result = func(self)
        return func.cached_result

    return new_func


# TODO extract this somewhere more generally available.
#  Borrow ideas from
#  https://github.com/mkorpela/overrides/blob/master/overrides/overrides.py
#  so docs and such can be given to the implementing classes. (That
#  library doesn't allow the specification of a super class, which I think
#  is critical).
def overrides(interface_class):
    """
    Override a parent class method.

    Assert that the method being overridden actually exists
    on the given class.
    """
    def overrider(method):
        assert(method.__name__ in dir(interface_class))
        return method
    return overrider


# TODO: Move to mycroft.util.time
def to_timestamp(dt_utc):
    return (dt_utc - datetime.datetime(1970, 1, 1)).total_seconds()


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
    """
    Skill to control Wink devices.

    Currently, only lights are supported.
    """
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

    @overrides(CommonIoTSkill)
    def get_entities(self):
        return self._entities.keys()

    @overrides(CommonIoTSkill)
    def get_scenes(self):
        return self._intensities.keys()

    def debug(self, message: str, level: int = 1, char: str = None):
        """
        Debugging assistance.

        Lower 'level' values are More important.  Only values less than the
        debug_level get logged.  Indentation is the inverse of this,
        with level 0 having 10 dashes as a prefix, 1 have 9, etc.
        """
        if level > WinkIoTSkill.debug_level:
            return

        if not char:
            char = "-"
        self.log.debug(char*(10-level) + " " + message)

    @property
    def room_name(self) -> str:
        """
        Assume the "name" of the device is the "room name"
        """
        device = DeviceApi().get()
        return device["name"]

    @cache
    def wink_devices(self) -> dict:
        """
        Retrieve a list of devices associated with the account.

        The result is cachced to avoid hitting the API. Use
        `wink_devices(use_cache=False)` to force a repull.
        """
        return self._winkapi_get("/users/me/wink_devices")

    @cache
    def wink_groups(self) -> dict:
        """
        Retrieve a list of groups of devices associated with the account.

        The result is cachced to avoid hitting the API. Use
        `wink_groups(use_cache=False)` to force a repull.
        """
        return self._winkapi_get("/users/me/groups")

    def set_light(self, entity: str, action: Action, brightness=1.0):
        """
        Set a and entity to a given state.

        :param entity: group or light name
        :param action: Action.ON will result in the lights being set to on.
                       All other actions will result to the lights being set
                       to off.
        :param brightness:

        :return: True if the request was made successfully.
        """
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

    def scale_lights(self, requst: IoTRequest, scale_by: float):
        """
        Scale the brightness of some lights.

        :param requst:
        :param scale_by:
        """
        try:
            entity = requst.entity or self.room_name
            light_ids = self._entities.get(entity)
            lights = [dev for dev in self.wink_devices(use_cache=False)["data"]
                      if dev.get("light_bulb_id") in light_ids]
            if lights:
                brightness = lights[0]["last_reading"]["brightness"] * scale_by
                self.set_light(entity, Action.ON, brightness)
            else:
                self.speak_dialog("couldnt.find.light")
        except Exception as e:
            LOG.exception(e)

    @overrides(CommonIoTSkill)
    def can_handle(self, request: IoTRequest):
        if not request.thing == Thing.LIGHT:
            return False, None
        if request.entity not in self._entities:
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

    @overrides(CommonIoTSkill)
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

    def _is_light(self, dev):
        """
        Determine if a wink device is a light bulb.

        :return: True if dev is a light bulb.
        """
        return "light_bulb_id" in dev

    def _initialize_entities(self):
        """
        Initialize self._entities with groups and lights.

        Groups and lights have names. This initializes
        self._entities to a dict of name to [device ids].

        Groups will take precedence over lights, if they
        have the same name.
        """
        groups = self.wink_groups().get("data")
        devices = self.wink_devices().get("data")
        groups = {group["name"]: [member["object_id"]
                                  for member in group["members"]
                                  if member["object_type"] == "light_bulb"]
                  for group in groups}
        lights = {dev["name"]: [dev["light_bulb_id"]]
                  for dev in devices if self._is_light(dev)}
        lights.update(groups)  # Groups take precedence
        self._entities = lights

    def _winkapi_auth(self):
        """
        Authenticate and obtain a token if necessary.

        :return: True if we're already logged in, or are able to
                 Successfully log in. False if the creds are not
                 supplied in the config.

        :raises Exception
                If we fail to log in.
        """
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

    def _winkapi_get(self, path: str):
        """
        Make a get request to wink.

        :param path: API path.
        :return: dict if we get a result,
                 False if we can't authenticate,
                 None if we don't get json back.

        """
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

    def _winkapi_put(self, path: str, body: dict):
        """
        Put to the wink API.

        :param path: API path.
        :param body: data to put
        :return: dict if we get a result
                 False if we can't authenticate
                 None if the request is unsuccessful.
        """
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


def create_skill():
    return WinkIoTSkill()
