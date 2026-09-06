# Demo-video seed artwork — sources and licences

Every file here is a **CC0 1.0** or **public-domain** photograph, downloaded from
Wikimedia Commons, centre-cropped, resized, re-encoded as progressive JPEG and
stripped of all EXIF/IPTC metadata. Logos are 1024×1024, covers 1600×900 (16:9,
the aspect ratio the frontend's `EventCoverImage` renders); every file is under
500 KB.

None of them contains a recognizable face, per the brand style guide's picture
rules (no stock models, no cold or distanced shots).

CC0 and public-domain images carry no attribution *requirement*. This file exists
so the provenance of a committed binary is auditable, and so the photographers get
credit anyway.

## Organization logos — `logos/`

| File | Source | Photographer | Licence |
| --- | --- | --- | --- |
| `shibari-circle-vienna.jpg` | [Cordage chantier de l'Hermione Rochefort sur Mer.jpg](https://commons.wikimedia.org/wiki/File:Cordage_chantier_de_l%27Hermione_Rochefort_sur_Mer.jpg) | Jebulon | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `the-velvet-cellar.jpg` | [Neon Lights (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Neon_Lights_(Unsplash).jpg) | Christy Ash | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `sunday-slow-picnic-club.jpg` | [Picnic basket (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Picnic_basket_(Unsplash).jpg) | Bonnie Kittle | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `analog-photo-walks.jpg` | [Vintage Nikon (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Vintage_Nikon_(Unsplash).jpg) | Jakob Owens | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `paper-hearts-book-club.jpg` | [Leather bound books (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Leather_bound_books_(Unsplash).jpg) | Chris Lawton | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

## Event cover art — `covers/`

| File | Source | Photographer | Licence |
| --- | --- | --- | --- |
| `intro-to-shibari-rope-and-trust.jpg` | [Rope Coil (20749461040).jpg](https://commons.wikimedia.org/wiki/File:Rope_Coil_(20749461040).jpg) | Glacier National Park (US National Park Service) | Public domain (work of the US federal government) |
| `basement-sessions-live-and-loud.jpg` | [Smoke and light beams (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Smoke_and_light_beams_(Unsplash).jpg) | Daniel Robert | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `picnic-in-the-park.jpg` | [Sharing is caring (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Sharing_is_caring_(Unsplash).jpg) | Toa Heftiba | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `golden-hour-photo-walk.jpg` | [Pedestrians at sunset (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Pedestrians_at_sunset_(Unsplash).jpg) | Jonas Weckschmied | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `monthly-reading-circle.jpg` | [Getting cozy with a book (Unsplash).jpg](https://commons.wikimedia.org/wiki/File:Getting_cozy_with_a_book_(Unsplash).jpg) | Alice Hampson | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

## Replacing or adding artwork

`artwork.py` finds a file by the row's own slug — `logos/<organization slug>.jpg`
and `covers/<event slug>.jpg` — so a new scenario needs a matching file here (and
a row in the tables above) or the seed raises `FileNotFoundError`.
