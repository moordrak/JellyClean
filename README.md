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

Skripta pita za:

- ID i hostname, OS predložak (Debian 12/13, Ubuntu 24.04), storage, disk, CPU, RAM i swap
- bridge, VLAN i DHCP ili statičku IP adresu
- vrstu kontejnera (unprivileged ili privileged)
- medijske mape: bind mount s hosta ili NFS mount unutar kontejnera (samo privileged)
- UID i GID vlasnika medija. Za unprivileged kontejner nudi 1:1 idmap, inače servis ne bi mogao brisati
- opcionalno Jellyfin config i cache mape, samo za čitanje
- Jellyfin URL i API ključ (oba odmah provjerava), korisnike koji se broje, provjeru TLS certifikata i `PATH_MAP` za svaku mapu
- brisanje, kantu, interval skeniranja, port, korisničko ime i lozinku (može se generirati)
- izvor koda: GitHub, GitHub s tokenom ili lokalne datoteke

Prije kreiranja prikazuje sažetak. Zatim skripta:

1. preuzme predložak, kreira kontejner i postavi mountove
2. instalira pakete i kreira korisnika
3. provjeri može li servis pisati u svaku medijsku mapu
4. podigne systemd servis i ispiše adresu i lozinku

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
