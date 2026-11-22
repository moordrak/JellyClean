# JellyClean

Web servis za pregled i čišćenje prostora na Jellyfin poslužitelju. Nema vanjskih ovisnosti, treba samo Python 3.9 ili noviji.

## Što radi

- **Pregled**: slobodan i zauzet prostor po disku, veličina svake medijske mape i njezinih najvećih podmapa, podjela po vrsti sadržaja (video, titlovi, slike, nfo) i po ekstenziji.
- **Preglednik**: prolaz kroz mape sortirane po veličini (kao `ncdu`), s mogućnošću označavanja i brisanja.
- **Najveće datoteke**: filtri po minimalnoj veličini, starosti, ekstenziji, dijelu putanje i mapi.
- **Jellyfin** (preko API-ja):
  - filmovi i serije s podatkom tko ih je pogledao, kad su zadnji put gledani i kad su dodani
  - filtri *pogledali svi korisnici*, *nitko nije pogledao* i *nije gledano X dana*
  - brisanje cijelog filma (mapa), cijele serije ili samo epizoda koje su pogledali svi
  - video datoteke koje postoje na disku, a Jellyfin ih ne poznaje
  - pokretanje Jellyfinovih zadataka održavanja (čišćenje cache i transcode mapa, logova itd.)
- **Smeće i duplikati**: sample videi, nedovršena preuzimanja (`.part`, `.!qB`), `.exe` i `.url` datoteke iz releasea, rar arhive, prazne datoteke i mape, više verzija istog filma ili epizode.
- **Kanta**: obrisane stavke se premještaju u `.jellyclean-trash` unutar iste mape, što je trenutno jer se ništa ne kopira. Iz kante se mogu vratiti, a automatski se prazni nakon `TRASH_DAYS` dana.
- **Hardlinkovi**: servis označava datoteke koje imaju još jednu kopiju (tipično *arr + qBittorrent) i upozorava da njihovo brisanje ne oslobađa prostor dok se ne obriše i torrent.
- Nakon brisanja Jellyfin sam pokreće osvježavanje biblioteke.

## Konfiguracija (varijable okruženja)

| Varijabla | Primjer | Opis |
|---|---|---|
| `MEDIA_PATHS` | `Filmovi=/media/movies,Serije=/media/tv` | Medijske mape koje se skeniraju i iz kojih se smije brisati |
| `JELLYFIN_DIRS` | `Config=/jellyfin/config,Cache=/jellyfin/cache` | Jellyfin mape koje se samo prikazuju, bez brisanja |
| `JELLYFIN_URL` | `http://192.168.1.10:8096` | Adresa Jellyfina |
| `JELLYFIN_API_KEY` | | Nadzorna ploča → API ključevi → + |
| `PATH_MAP` | `/data/movies=/media/movies;/data/tv=/media/tv` | Kad Jellyfin vidi medije pod drugom putanjom nego JellyClean, format je `jellyfin=jellyclean` |
| `JELLYFIN_USERS` | `ivan,ana` | Koji se korisnici broje za *pogledali svi*. Prazno znači svi aktivni korisnici |
| `ALLOW_DELETE` | `true` | `false` uključuje način samo za pregled |
| `TRASH_MODE` | `true` | `false` znači da je brisanje odmah trajno |
| `TRASH_DAYS` | `7` | Nakon koliko dana se kanta automatski prazni (`0` znači nikad) |
| `SCAN_INTERVAL_HOURS` | `6` | Koliko često se automatski ponovno skenira |
| `AUTH_USER` / `AUTH_PASS` | | Basic auth. **Postavi ga**, jer sučelje može brisati datoteke |
| `PORT` | `8095` | |
| `JELLYFIN_VERIFY_TLS` | `true` | `false` za samopotpisani certifikat (npr. `.int` domena) |

## Pokretanje

### Docker

Uredi `docker-compose.yml` (volumene, URL i ključ), zatim pokreni:

```bash
docker compose up -d --build
```

`user:` mora biti isti UID i GID koji imaju medijske datoteke, inače brisanje javlja *Permission denied*.

### Proxmox: novi LXC jednom naredbom (preporučeno)

Na Proxmox **hostu** kao root:

```bash
# javni repozitorij
bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/create-lxc.sh)"

# privatni repozitorij: kloniraj ga na host pa pokreni lokalno
git clone https://github.com/moordrak/JellyClean.git && bash JellyClean/proxmox/create-lxc.sh
```

Skripta kreira **privileged** LXC i NFS shareove s NAS-a montira **unutar kontejnera** (`/etc/fstab` u kontejneru). Na Proxmox host se ne dodaje ništa.

Većinu postavki otkriva sama, a prije svake promjene pojasni prijedlog i traži odobrenje:

| Korak | Što se otkriva | Kako |
|---|---|---|
| Kontejner | ID, storage s najviše mjesta, bridge, resursi | predlaže se sve odjednom; možeš prihvatiti ili mijenjati |
| NFS | exporti na NAS-u | `pvesm scan nfs` ili `showmount -e` (samo čitanje). Exporti s imenom koje upućuje na medije unaprijed su označeni. Ako NAS nudi samo NFSv4, upisuju se ručno |
| Biblioteke | Jellyfin biblioteke i njihove putanje | Jellyfin API (`/Library/VirtualFolders`) |
| Povezivanje | koja mapa na NAS-u odgovara kojoj biblioteci | nekoliko stvarnih datoteka iz Jellyfina traži se na montiranim shareovima (npr. „✓ 8/8 datoteka”). Radi i kad se mape zovu različito (`/data/tvshows` ↔ `/mnt/nas/media/tv`) |
| `PATH_MAP` | Jellyfin putanja → putanja u kontejneru | iz povezivanja |
| Korisnik | UID i GID vlasnika medija | iz vlasnika pronađenih datoteka. Upozorava na `nobody` (squash) |
| Prava | može li servis brisati | test pisanja kao taj korisnik u svakoj mapi |

Bez Jellyfina skripta predlaže mape prema imenu (`movies`, `filmovi`, `tv`, `serije`, `anime`…). Biblioteka koja nije pronađena može se ručno povezati s mapom.

Ručno se upisuju samo IP adresa NAS-a, Jellyfin URL i API ključ. Sve ostalo ima prijedlog.

Ako se NFS export ne montira, najčešći uzrok je da NAS ne dopušta IP adresu kontejnera. Dodaj je u NFS dozvole na NAS-u.

Upravljanje kontejnerom:

```bash
pct exec <ID> -- nano /etc/jellyclean.env      # konfiguracija, pa: systemctl restart jellyclean
pct exec <ID> -- journalctl -u jellyclean -f   # logovi
pct exec <ID> -- jellyclean-update             # nadogradnja (privatni repo: GH_TOKEN=xxx jellyclean-update)
```

### Postojeći LXC

```bash
# unutar LXC-a, s već montiranim medijima
bash proxmox/install-in-lxc.sh
nano /etc/jellyclean.env && systemctl restart jellyclean
```

### Ručno

```bash
MEDIA_PATHS="Filmovi=/mnt/media/movies" JELLYFIN_URL=http://jf:8096 JELLYFIN_API_KEY=xxx python3 app.py
```

## Napomene

- **Radarr/Sonarr**: ako je film ili serija praćen (*monitored*), *arr će ga nakon brisanja ponovno preuzeti. Prije brisanja ga isključi iz praćenja ili ga obriši kroz sam Radarr/Sonarr.
- Skener čita samo metapodatke datoteka i ne otvara sam video. Na velikim NFS mapama prvo skeniranje ipak može potrajati nekoliko minuta.
- Kanta je na istom disku, pa se prostor stvarno oslobađa tek kad se kanta isprazni.
- POST zahtjevi traže header `X-Requested-With: JellyClean` kao zaštitu od CSRF-a.
