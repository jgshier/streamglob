import logging
logger = logging.getLogger(__name__)

import urwid

from orderedattrdict import AttrDict
from panwid.datatable import *
from pony.orm import *
import requests
import dateutil.parser

from .. import session

from .base import *
from .bam import *
from .filters import *

from .. import model
from ..exceptions import *
from ..state import *

class MLBLineScoreDataTable(BAMLineScoreDataTable):

    SCORING_ATTRS = ["runs", "hits", "errors"]
    PLAYING_PERIOD_ATTR = "innings"
    NUM_PLAYING_PERIODS = 9

    @classmethod
    def PLAYING_PERIOD_DESC(cls, line_score):
        return f"""{line_score.get("inningHalf")[:3]} {line_score.get("currentInningOrdinal")}"""

class MLBHighlightsDataTable(HighlightsDataTable):

    columns = [
        DataTableColumn("inning", width=5,
                        value = lambda t, r: r.data.attrs.inning),
        # DataTableColumn("top_play", width=5,
        #                 value = lambda t, r: r.data.attrs.top_play),
    ] + HighlightsDataTable.COLUMNS


class MLBDetailBox(BAMDetailBox):

    HIGHLIGHT_TABLE_CLASS = MLBHighlightsDataTable

    EVENT_TYPES = AttrDict(
        hitting="H",
        pitching="P",
        defense="F",
        baserunning="R"
    )

    @property
    def HIGHLIGHT_ATTR(self):
        return "highlights"

    def get_highlight_attrs(self, highlight, listing):

        timestamp = None
        running_time = None
        event_type = None
        inning = None

        plays = listing.plays
        keywords = highlight.get("keywordsAll", None)

        game_start = dateutil.parser.parse(
            listing.game_data["gameDate"]
        )

        try:
            event_id = next(k["value"] for k in keywords if k["type"] == "sv_id")
        except StopIteration:
            event_id = None

        try:
            play, event = next( (p, pe) for p in plays
                        for pe in p["playEvents"]
                        if event_id and pe.get("playId", None) == event_id)
        except StopIteration:
            play = None
            event = None

        if play:
            event_type = play["result"].get("event", None)

            timestamp = dateutil.parser.parse(play["about"].get(
                    "startTime", None)
            ).astimezone(
                pytz.timezone(config.settings.profile.time_zone)
            )

            running_time = timestamp - game_start
            inning = f"{play['about']['halfInning'][:3].title()} {play['about']['inning']}"

        if not event_type:
            if any((k["type"] == "mlbtax"
                   and k["displayName"] == "Interview"
                   for k in keywords)):
                event_type = "Interview"
            elif any((k["type"] == "mlbtax"
                   and k["displayName"] == "Managers"
                   for k in keywords)):
                event_type = "Postgame"
            elif any((k["type"] == "mlbtax"
                   and k["displayName"] == "Managers"
                   for k in keywords)):
                event_type = "News Conference"
            else:
                event_type = "Other"

        return AttrDict(
            timestamp = timestamp,
            running_time = running_time,
            event_type = event_type,
            inning = inning
            # top_play = top_play,
            # description = play["result"].get("description", None),
        )

    def __repr__(self):
        return ""

@dataclass
class MLBMediaListing(BAMMediaListing):

    @property
    @memo(region="short")
    def line(self):
        style = self.provider.config.listings.line.style
        table = MLBLineScoreDataTable.for_game(
            self.provider, self.game_data, self.hide_spoilers,
            # style = style
        )
        return BAMLineScoreBox(table, style)


class MLBBAMProviderData(BAMProviderData):
    pass

class MLBStreamSession(session.AuthenticatedStreamSession):

    PLATFORM = "macintosh"
    BAM_SDK_VERSION = "3.0"

    API_KEY_URL = "https://www.mlb.com/tv/g490865/"

    API_KEY_RE = re.compile(r'"apiKey":"([^"]+)"')

    CLIENT_API_KEY_RE = re.compile(r'"clientApiKey":"([^"]+)"')

    TOKEN_URL_TEMPLATE = (
        "https://media-entitlement.mlb.com/jwt"
        "?ipid={ipid}&fingerprint={fingerprint}==&os={platform}&appname=mlbtv_web"
    )

    GAME_CONTENT_URL_TEMPLATE="http://statsapi.mlb.com/api/v1/game/{game_id}/content"

    ACCESS_TOKEN_URL = "https://edge.bamgrid.com/token"

    STREAM_URL_TEMPLATE="https://edge.svcs.mlb.com/media/{media_id}/scenarios/browser"

    AIRINGS_URL_TEMPLATE=(
        "https://search-api-mlbtv.mlb.com/svc/search/v2/graphql/persisted/query/"
        "core/Airings?variables={{%22partnerProgramIds%22%3A[%22{game_id}%22]}}"
    )

    def __init__(
            self,
            provider_id,
            username, password,
            api_key=None,
            client_api_key=None,
            token=None,
            access_token=None,
            access_token_expiry=None,
            *args, **kwargs
    ):
        super(MLBStreamSession, self).__init__(
            provider_id,
            username, password,
            *args, **kwargs
        )
        self._state.api_key = api_key
        self._state.client_api_key = client_api_key
        self._state.token = token
        self._state.access_token = access_token
        self._state.access_token_expiry = access_token_expiry


    def login(self):

        if self.logged_in:
            logger.debug("already logged in")
            return

        # logger.debug("checking for existing log in")

        initial_url = ("https://secure.mlb.com/enterworkflow.do"
                       "?flowId=registration.wizard&c_id=mlb")

        data = {
            "uri": "/account/login_register.jsp",
            "registrationAction": "identify",
            "emailAddress": self.username,
            "password": self.password,
            "submitButton": ""
        }
        logger.debug("attempting new log in")

        login_url = "https://securea.mlb.com/authenticate.do"

        res = self.post(
            login_url,
            data=data,
            headers={"Referer": (initial_url)}
        )

        if not (self.ipid and self.fingerprint):
            # print(res.content)
            raise SGStreamSessionException("Couldn't get ipid / fingerprint")

        logger.info("logged in: %s" %(self.ipid))
        self.save()

    @property
    def logged_in(self):

        logged_in_url = ("https://web-secure.mlb.com/enterworkflow.do"
                         "?flowId=registration.newsletter&c_id=mlb")
        content = self.get(logged_in_url).text
        parser = lxml.etree.HTMLParser()
        data = lxml.etree.parse(StringIO(content), parser)
        if "Login/Register" in data.xpath(".//title")[0].text:
            return False

    @property
    def headers(self):

        return {
            "Authorization": self.access_token
        }


    @property
    def ipid(self):
        return self.get_cookie("ipid")

    @property
    def fingerprint(self):
        return self.get_cookie("fprt")

    @property
    def api_key(self):

        if not self._state.get("api_key"):
            self.update_api_keys()
        return self._state.api_key

    @property
    def client_api_key(self):

        if not self._state.get("client_api_key"):
            self.update_api_keys()
        return self._state.client_api_key

    def update_api_keys(self):

        logger.debug("updating api keys")
        content = self.get("https://www.mlb.com/tv/g490865/").text
        parser = lxml.etree.HTMLParser()
        data = lxml.etree.parse(StringIO(content), parser)

        scripts = data.xpath(".//script")
        for script in scripts:
            if script.text and "apiKey" in script.text:
                self._state.api_key = self.API_KEY_RE.search(script.text).groups()[0]
            if script.text and "clientApiKey" in script.text:
                self._state.client_api_key = self.CLIENT_API_KEY_RE.search(script.text).groups()[0]
        self.save()

    @property
    def token(self):
        if not self._state.token:
            logger.debug("getting token")
            headers = {"x-api-key": self.api_key}

            response = self.get(
                self.TOKEN_URL_TEMPLATE.format(
                    ipid=self.ipid, fingerprint=self.fingerprint,
                    platform=self.PLATFORM
                ),
                headers=headers
            )
            self._state.token = response.text
        return self._state.token

    @token.setter
    def token(self, value):
        self._state.token = value

    @property
    def access_token_expiry(self):

        if self._state.access_token_expiry:
            return dateutil.parser.parse(self._state.access_token_expiry)

    @access_token_expiry.setter
    def access_token_expiry(self, val):
        if val:
            self._state.access_token_expiry = val.isoformat()

    @property
    def access_token(self):
        if not self._state.access_token or not self.access_token_expiry or \
                self.access_token_expiry < datetime.now(tz=pytz.UTC):
            try:
                self.refresh_access_token()
            except requests.exceptions.HTTPError:
                # Clear token and then try to get a new access_token
                self.refresh_access_token(clear_token=True)

        logger.debug("access_token: %s" %(self._state.access_token))
        return self._state.access_token

    def refresh_access_token(self, clear_token=False):
        if not self.logged_in:
            self.login()
        logger.debug("refreshing access token")
        if clear_token:
            self.token = None
        headers = {
            "Authorization": "Bearer %s" % (self.client_api_key),
            "User-agent": USER_AGENT,
            "Accept": "application/vnd.media-service+json; version=1",
            "x-bamsdk-version": self.BAM_SDK_VERSION,
            "x-bamsdk-platform": self.PLATFORM,
            "origin": "https://www.mlb.com"
        }
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "platform": "browser",
            "setCookie": "false",
            "subject_token": self.token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"
        }
        response = self.post(
            self.ACCESS_TOKEN_URL,
            data=data,
            headers=headers
        )
        # from requests_toolbelt.utils import dump
        # print(dump.dump_all(response).decode("utf-8"))
        response.raise_for_status()
        token_response = response.json()

        self.access_token_expiry = datetime.now(tz=pytz.UTC) + \
                       timedelta(seconds=token_response["expires_in"])
        self._state.access_token = token_response["access_token"]
        self.save()

    def content(self, game_id):

        return self.get(
            self.GAME_CONTENT_URL_TEMPLATE.format(game_id=game_id)).json()


    def airings(self, game_id):

        airings_url = self.AIRINGS_URL_TEMPLATE.format(game_id = game_id)
        airings = self.get(
            airings_url
        ).json()["data"]["Airings"]
        return airings


    def get_stream(self, media):

        # media_id = media.get("mediaId", media.get("guid"))
        # logger.info(media_id)

        headers={
            "Authorization": self.access_token,
            "User-agent": USER_AGENT,
            "Accept": "application/vnd.media-service+json; version=1",
            "x-bamsdk-version": "3.0",
            "x-bamsdk-platform": self.PLATFORM,
            "origin": "https://www.mlb.com"
        }
        stream_url = self.STREAM_URL_TEMPLATE.format(media_id=media.media_id)
        logger.info("getting stream %s" %(stream_url))
        stream = self.get(
            stream_url,
            headers=headers
        ).json()
        logger.debug("stream response: %s" %(stream))
        if "errors" in stream and len(stream["errors"]):
            raise SGStreamNotFound(stream["errors"])
        stream = AttrDict(stream)
        stream.url = stream["stream"]["complete"]
        return stream


class MLBProvider(BAMProviderMixin,
                  BaseProvider):

    SESSION_CLASS = MLBStreamSession

    MEDIA_TYPES = {"video"}

    RESOLUTIONS = AttrDict([
        ("720p", "720p_alt"),
        ("720p@30", "720p"),
        ("540p", "540p"),
        ("504p", "504p"),
        ("360p", "360p"),
        ("288p", "288p"),
        ("224p", "224p")
    ])

    SCHEDULE_TEMPLATE = (
        "http://statsapi.mlb.com/api/v1/schedule"
        "?sportId={sport_id}&startDate={start}&endDate={end}"
        "&gameType={game_type}&gamePk={game_id}"
        "&teamId={team_id}"
        "&hydrate=linescore,team,game(content(summary,media(epg),"
        "highlights(highlights(items))))"
    )

    SCHEDULE_TEMPLATE_BRIEF = (
        "http://statsapi.mlb.com/api/v1/schedule"
        "?sportId={sport_id}&startDate={start}&endDate={end}"
        "&gameType={game_type}&gamePk={game_id}"
        "&teamId={team_id}"
    )

    GAME_DATA_TEMPLATE = (
        "http://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
    )

    # DATA_TABLE_CLASS = MLBLineScoreDataTable

    MEDIA_TITLE = "MLBTV"

    MEDIA_ID_FIELD = "mediaId"

    DETAIL_BOX_CLASS = MLBDetailBox

    @classproperty
    def NAME(cls):
        return "MLB.tv"

    # @classmethod
    # def config_is_valid(cls, cfg):
    #     return all(c


    def teams(self, sport_code="mlb", season=None):

        if sport_code != "mlb":
            media_title = "MiLBTV"
            raise SGException("Sorry, MiLB.tv streams are not yet supported")

        sports_url = (
            "http://statsapi.mlb.com/api/v1/sports"
        )
        with self.session.cache_responses_long():
            sports = self.session.get(sports_url).json()

        sport = next(s for s in sports["sports"] if s["code"] == sport_code)

        # season = game_date.year
        teams_url = (
            "http://statsapi.mlb.com/api/v1/teams"
            "?sportId={sport}&{season}".format(
                sport=sport["id"],
                season=season if season else ""
            )
        )

        # raise Exception(state.session.get(teams_url).json())

        with self.session.cache_responses_long():
            teams = AttrDict(
                (team["abbreviation"].lower(), team["id"])
                for team in sorted(self.session.get(teams_url).json()["teams"],
                                   key=lambda t: t["fileCode"])
            )

        return teams

    @property
    @db_session
    def start_date(self):

        now = datetime.now()
        year = now.year
        season_year = (now - relativedelta(months=2)).year

        r = MLBBAMProviderData.get(season_year=season_year)
        if r:
            start = r.start
            end = r.end
        else:
            schedule = self.schedule(
                start=datetime(year, 1, 1),
                end=datetime(year, 12, 31),
                brief=True
            )
            start = dateutil.parser.parse(schedule["dates"][0]["date"])
            end = dateutil.parser.parse(schedule["dates"][-1]["date"])
            r = MLBBAMProviderData(
                season_year=season_year,
                start = start,
                end = end
            )

        if now < start:
            return start.date()
        elif now > end:
            return end.date()
        else:
            return now.date()


    def media_timestamps(self, game_id, media_id):

        try:
            # try to get the precise timestamps for this stream
            airing = next(a for a in self.session.airings(game_id)
                          if len(a["milestones"])
                          and a["mediaId"] == media_id)
        except StopIteration:
            # welp, no timestamps -- try to get them from whatever feed has them
            try:
                airing = next(a for a in self.session.airings(game_id)
                            if len(a["milestones"]))
            except StopIteration:
                logger.warning(SGStreamSessionException(
                    "No airing for media %s" %(media_id))
                )
                return AttrDict([("Start", 0)])

        start_timestamps = []
        try:
            start_time = next(
                    t["startDatetime"] for t in
                    next(m for m in airing["milestones"]
                     if m["milestoneType"] == "BROADCAST_START"
                    )["milestoneTime"]
                if t["type"] == "absolute"
                )

        except StopIteration:
            # Some streams don't have a "BROADCAST_START" milestone.  We need
            # something, so we use the scheduled game start time, which is
            # probably wrong.
            start_time = airing["startDate"]

        start_timestamps.append(
            ("S", start_time)
        )

        try:
            start_offset = next(
                t["start"] for t in
                next(m for m in airing["milestones"]
                     if m["milestoneType"] == "BROADCAST_START"
                )["milestoneTime"]
                if t["type"] == "offset"
            )
        except StopIteration:
            # Same as above.  Missing BROADCAST_START milestone means we
            # probably don't get accurate offsets for inning milestones.
            start_offset = 0

        start_timestamps.append(
            ("SO", start_offset)
        )

        timestamps = AttrDict(start_timestamps)
        timestamps.update(AttrDict([
            (
            "%s%s" %(
                "T"
                if next(
                        k for k in m["keywords"]
                        if k["type"] == "top"
                )["value"] == "true"
                else "B",
                int(
                    next(
                        k for k in m["keywords"] if k["type"] == "inning"
                    )["value"]
                )),
            next(t["start"]
                      for t in m["milestoneTime"]
                      if t["type"] == "offset"
                 )
            )
                 for m in airing["milestones"]
                 if m["milestoneType"] == "INNING_START"
        ]))
        return timestamps




    # def get_highlights(self, selection):

    #     game_id = selection.game_id
    #     game = self.game_data(game_id)

        return [ AttrDict(dict(
            media_id = h["guid"],
            title = h["title"],
            url = next(p for p in h["playbacks"] if p["name"] == "HTTP_CLOUD_WIRED_60"),
            h=h
        )) for h in j["highlights"]["highlights"]["items"] ]



    # def get_stream(self, media):

    #     media_id = media.get("mediaId", media.get("guid"))

    #     headers={
    #         "Authorization": self.session.access_token,
    #         # "User-agent": USER_AGENT,
    #         "Accept": "application/vnd.media-service+json; version=1",
    #         "x-bamsdk-version": "3.0",
    #         "x-bamsdk-platform": self.PLATFORM,
    #         "origin": "https://www.mlb.com"
    #     }
    #     stream_url = self.STREAM_URL_TEMPLATE.format(media_id=media_id)
    #     logger.info("getting stream %s" %(stream_url))
    #     stream = self.get(
    #         stream_url,
    #         headers=headers
    #     ).json()
    #     logger.debug("stream response: %s" %(stream))
    #     if "errors" in stream and len(stream["errors"]):
    #         return None
    #     stream = Stream(stream)
    #     stream.url = stream["stream"]["complete"]
    #     return stream




# register_provider(MLBProvider)
