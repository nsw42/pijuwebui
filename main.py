from argparse import ArgumentParser
from collections import defaultdict, namedtuple
from itertools import zip_longest
import logging
import string
from typing import List, Optional

from flask import Flask, abort, render_template
import requests
import unidecode

import genre_view

app = Flask(__name__)

Album = namedtuple('Album', 'id, artist, title, year, artwork_link, genre_name, numberdisks, tracks, anchor')
Artist = namedtuple('Artist', 'name, albums')
Track = namedtuple('Track', 'id, title, disknumber, tracknumber')


def id_from_link(link):
    return link[link.rindex('/') + 1:]


class Cache:
    def __init__(self):
        # The following instance variables are populated by ensure_genre_cache()
        self.display_genres = None
        self.genre_links = None
        self.genre_names_from_links = {}
        # The following instance variables are populated by ensure_genre_contents_cache(genre_name)
        self.albums_in_genre = defaultdict(list)  # map from genre_name to list of Album
        # The following instance variables are populated by ensure_album_cache(album_id)
        self.album_details = {}
        # The following instance variables are populated by ensure_artist_cache(artist)
        self.artist_details = {}

    def ensure_genre_cache(self):
        app.logger.debug("ensure_genre_cache")
        if self.display_genres is None:
            response = requests.get(app.server + '/genres')
            if response.status_code != 200:
                raise Exception('Unable to connect to server')  # TODO: Error handling
            display_names_set = set()
            self.genre_links = defaultdict(list)  # map from genre displayed name to list of server address
            server_genre_json = response.json()
            for server_genre in server_genre_json:
                server_link = server_genre['link']
                server_genre_name = server_genre['name']
                display_genre = genre_view.GENRE_LOOKUP.get(server_genre_name)
                if display_genre:
                    display_genre = display_genre.displayed_name
                else:
                    print(f"WARNING: Do not know how to categorise {server_genre_name} ({server_genre['link']})")
                    display_genre = genre_view.UNCATEGORISED
                self.genre_links[display_genre].append(server_link)
                app.logger.debug(f'{server_link} -> {display_genre}')
                self.genre_names_from_links[server_link] = display_genre
                display_names_set.add(display_genre)
            self.display_names = list(sorted(display_names_set, key=lambda dn: genre_view.GENRE_SORT_ORDER[dn]))
            self.display_genres = [genre_view.GENRE_VIEWS[dn] for dn in self.display_names]

    def ensure_genre_contents_cache(self, genre_name) -> Optional[List[Album]]:
        """
        Ensure we have a cache of the contents of the given genre,
        and return a list of the albums in that Genre.
        Albums are sorted by artist then release year, and finally title
        """
        self.ensure_genre_cache()
        if self.albums_in_genre.get(genre_name) is None:
            server_links = self.genre_links.get(genre_name)  # Could be a request for an unknown genre name
            if server_links is None:
                return None
            albums = {}  # indexed by album id to avoid duplication
            # (eg for the scenario of an album in two genres, both of which are displayed under the same
            # genre displayname)
            for link in server_links:
                response = requests.get(app.server + link + '?albums=all')
                if response.status_code != 200:
                    abort(500)  # TODO: Error handling
                genre_json = response.json()
                for album_json in genre_json['albums']:
                    album = self.add_album_from_json(album_json)
                    albums[album.id] = album

            def get_album_sort_order(album):
                artist = album.artist if album.artist else "Unknown Artist"
                artist = artist.replace('"', '')
                artist = unidecode.unidecode(artist)
                artist = artist.lower()
                title = album.title if album.title else "ZZZZZZZZZZZ"
                title = unidecode.unidecode(title)
                title = title.lower()
                return (artist, album.year or 0, title)
            albums = list(albums.values())
            albums.sort(key=get_album_sort_order)
            self.albums_in_genre[genre_name] = albums
        return self.albums_in_genre[genre_name]

    def ensure_album_cache(self, album_id) -> Optional[Album]:
        self.ensure_genre_cache()  # Needed for the genre_name in add_album_from_json
        if self.album_details.get(album_id) is None:
            response = requests.get(f'{app.server}/albums/{album_id}?tracks=all')
            if response.status_code != 200:
                abort(500)  # TODO: Error handling
            self.add_album_from_json(response.json())  # updates self.album_details[album_id]
        return self.album_details[album_id]

    def ensure_artist_cache(self, artist) -> Optional[Artist]:
        self.ensure_genre_cache()  # Needed for the genre_name in add_album_from_json
        artist_lookup = artist.lower()
        if self.artist_details.get(artist_lookup) is None:
            response = requests.get(f'{app.server}/artists/{artist}?tracks=all')
            if response.status_code != 200:
                abort(404)  # TODO: Error handling
            self.add_artist_from_json(response.json())  # updates self.artist_details[artist.lower()]
        return self.artist_details[artist_lookup]

    def add_album_from_json(self, album_json):
        album_id = id_from_link(album_json['link'])
        first_genre = album_json['genres'][0] if album_json['genres'] else None
        genre_name = self.genre_names_from_links[first_genre] if first_genre else None
        artist = album_json['artist']
        anchor = artist[0].upper() if artist else 'U'
        anchor = unidecode.unidecode(anchor)
        if anchor not in string.ascii_uppercase:
            anchor = 'num'
        tracks = [Track(id=id_from_link(track_json['link']),
                        title=track_json['title'],
                        disknumber=track_json['disknumber'],
                        tracknumber=track_json['tracknumber'])
                  for track_json in album_json['tracks']]
        tracks.sort(key=lambda t: (t.disknumber if (t.disknumber is not None) else 9999,
                                   t.tracknumber if (t.tracknumber is not None) else 0))
        album_details = Album(id=album_id,
                              artist=artist,
                              title=album_json['title'],
                              year=album_json['releasedate'],
                              artwork_link=album_json['artwork']['link'],
                              genre_name=genre_name,
                              numberdisks=album_json['numberdisks'],
                              tracks=tracks,
                              anchor=anchor)
        self.album_details[album_id] = album_details
        return album_details

    def add_artist_from_json(self, artist_json):
        # We'd normally only expect a single artist in the response
        # but this works if there are multiple, which can happen if
        # there are multiple capitalisations of an artist
        albums = []
        for artist_name, albums_json in artist_json.items():
            for album_json in albums_json:
                albums.append(self.add_album_from_json(album_json))
        albums.sort(key=lambda album: album.year if album.year else 9999)
        artist = Artist(artist_name, albums)
        self.artist_details[artist_name.lower()] = artist


@app.route("/")
def root():
    app.cache.ensure_genre_cache()
    return render_template('index.html', **app.default_template_args, genres=app.cache.display_genres)


@app.route("/genre/<genre_name>")
def get_genre(genre_name):
    albums = app.cache.ensure_genre_contents_cache(genre_name)
    if albums is None:
        abort(404)
    anchors = set(album.anchor for album in albums)
    letters = "#" + string.ascii_uppercase
    anchor_names = ['num'] + list(string.ascii_uppercase)
    have_anchors = dict([(anchor, anchor in anchors) for anchor in anchor_names])
    first_album_for_anchor = {}
    for album in albums:
        if album.anchor not in first_album_for_anchor:
            first_album_for_anchor[album.anchor] = album.id
    return render_template('genre.html', **app.default_template_args,
                           genre_name=genre_name,
                           albums=albums,
                           letters=letters,
                           have_anchors=have_anchors,
                           first_album_for_anchor=first_album_for_anchor)


@app.route("/albums/<album_id>")
def get_album(album_id):
    album = app.cache.ensure_album_cache(album_id)
    if album is None:
        abort(404)
    return render_template('album.html', **app.default_template_args, album=album)


@app.route("/artists/<artist>")
def get_artist(artist):
    artist = app.cache.ensure_artist_cache(artist)
    if artist is None:
        abort(404)
    return render_template('artist.html', **app.default_template_args, artist=artist)


@app.route("/play/<album_id>/<track_id>", methods=["POST"])
def play(album_id, track_id):
    requests.post(f"{app.server}/player/play",
                  json={'album': album_id, 'track': track_id})
    return ('', 204)


@app.route("/search")
def search():
    return render_template('search.html', **app.default_template_args)


def make_header(links):
    return ' | '.join(make_header_component(dest, label) for (dest, label) in links)


def make_header_component(dest, label):
    return f'<a href="{dest}" class="text-decoration-none" style="color: ghostwhite">{label}</a>'


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('server', type=str, nargs='?',
                        help="Piju server hostname or IP address. "
                             "Port may optionally be specified as :PORT")
    parser.set_defaults(server='piju:5000')
    args = parser.parse_args()
    # TODO: Error checking on args.server
    if not args.server.startswith('http'):
        args.server = 'http://' + args.server
    if args.server[-1] == '/':
        args.server = args.server[:-1]
    return args


def connection_test(server, required_api_version):
    try:
        response = requests.get(server + "/")
    except requests.exceptions.ConnectionError:
        logging.error("Unable to connect to the specified server")
        return
    if response.status_code != 200:
        logging.warning("Unable to connect to the specified server")
        return
    data = response.json()
    api_version = data.get('ApiVersion')
    if not api_version:
        logging.warning("Server response did not include an API protocol version. "
                        "Probably an old or incompatible server")
        return
    required_api_version_fragments = required_api_version.split('.')
    detected_api_version_fragments = api_version.split('.')
    for (required_fragment, detected_fragment) in zip_longest(required_api_version_fragments,
                                                              detected_api_version_fragments,
                                                              fillvalue='0'):
        if not required_fragment.isdigit() or not detected_fragment.isdigit():
            logging.warning("Non-numeric version string fragment encountered")  # NB not fully semver compatible
            continue
        required_fragment = int(required_fragment)
        detected_fragment = int(detected_fragment)
        if required_fragment == detected_fragment:
            pass
        elif required_fragment < detected_fragment:
            msg = "Server is using a newer protocol version than the UI requires: may be incompatible. "
            msg += f"Required: {required_api_version}; detected: {api_version}"
            logging.warning(msg)
            return
        elif required_fragment > detected_fragment:
            msg = "Server is using an older protocol version than required by the UI: likely to be incompatible. "
            msg += f"Required: {required_api_version}; detected: {api_version}"
            logging.error(msg)
            return


def main():
    args = parse_args()
    app.server = args.server
    app.cache = Cache()
    app.default_template_args = {
        "server": app.server,
        "make_header": make_header
    }
    connection_test(app.server, required_api_version='2.0')
    app.run(host='0.0.0.0', port=80, debug=True)


if __name__ == '__main__':
    main()
