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
- **Jellyfin config** (preko SSH-a do računala na kojem radi Jellyfin):
  - zauzeće mapa `cache`, `cache/images`, `cache/transcodes`, `log`, `data/metadata` i baze
  - čišćenje slika u cacheu, ostataka transkodiranja (starijih od 1 sata), ostatka cachea i logova starijih od X dana, uvijek s pregledom prije brisanja
  - **siročad u `metadata/library`**: mape filmova i epizoda koje više ne postoje. Uspoređuju se s Jellyfin API-jem, a svaka se još pojedinačno provjerava prije brisanja
  - baza (`*.db`) se nikad ne dira
- **Provjera putanja**: uspoređuje putanje u Jellyfin bazi sa stvarnim datotekama.
  - Ako na zapisanoj putanji nema datoteke, traži je drugdje: isto ime, druga velika i mala slova, drugačiji Unicode zapis znakova (č/ć/ž), ista veličina, ista oznaka SxxEyy u mapi serije.
  - Prepoznaje i epizode koje Jellyfin ima bez datoteke, iako ona postoji (poruka *Unable to find a valid media source to play*).
  - Kad se ista promjena putanje ponavlja kod više stavki, javlja da je vjerojatno promijenjen mount ili putanja biblioteke.
  - **Siguran ispravak:** Jellyfinu se preko API-ja javi promjena (`/Library/Media/Updated`) i osvježi se serija.
  - **Ispravak u bazi** (uz SSH pristup): zaustavi Jellyfin, napravi kopiju `jellyfin.db`, promijeni samo putanje odabranih zapisa (ID i gledanost ostaju) i ponovno ga pokrene. Ako struktura baze nije prepoznata, ništa se ne mijenja.
- **Zašto se ne pušta?** Dijagnoza reprodukcije za odabrani film ili epizodu, preko pretrage ili gumba *Zašto?* u Provjeri putanja:
  - zapis u bazi, izvori (MediaSources) i `PlaybackInfo` iz Jellyfina
  - datoteka na NAS-u i traženje datoteke drugdje
  - preko SSH-a: vidi li datoteku i može li je čitati **sam Jellyfin kontejner**, `ffprobe` provjera sadržaja te greške za tu stavku iz Jellyfin i FFmpeg logova
  - jasan zaključak (zapis bez datoteke, kriva putanja, mount, dozvole, oštećena datoteka, neuspjelo transkodiranje, problem do klijenta) i popravak jednim klikom (osvježi stavku ili javi Jellyfinu datoteku)
- **Test paralelnog enkodiranja**: koliko istovremenih transkodiranja NVIDIA kartica u Jellyfinu može odraditi.
- **Nadzor kartice uživo** (na kartici Test enkodiranja): GPU, ENC, DEC, VRAM, temperatura, snaga, ventilator i takt u stvarnom vremenu, graf zadnjih 5 minuta i popis procesa na kartici (koja se datoteka transkodira, u što, koliko ENC/DEC/VRAM troši). Isti NVML podaci koje prikazuje nvtop.
- **NVENC patch drivera**: uklanja ograničenje broja istovremenih NVENC sesija na GeForce karticama ([keylase/nvidia-patch](https://github.com/keylase/nvidia-patch)), uz sigurnosnu kopiju, vraćanje originala, provjeru s 13 istovremenih sesija i upozorenje kad nadogradnja drivera poništi patch.
  - testnu datoteku biraš iz biblioteke, a test se preko SSH-a izvodi **u Jellyfin kontejneru** s Jellyfinovim ffmpegom (NVDEC → `scale_cuda` → `h264_nvenc`)
  - broj istovremenih tokova raste korak po korak, uz praćenje brzine svakog toka te opterećenja ENC, DEC, GPU i VRAM
  - dva načina: *maksimalna brzina* (granica kartice i drivera) i *stvarno vrijeme* (opterećenje kao kod gledanja)
  - rezultat: koliko tokova kartica stiže, preporučeni broj s rezervom i što ju je ograničilo (ograničenje sesija drivera ili snaga)
  - upozorava ako netko upravo gleda s transkodiranjem; test se može prekinuti
- **Smeće i duplikati**: sample videi, nedovršena preuzimanja (`.part`, `.!qB`), `.exe` i `.url` datoteke iz releasea, rar arhive, prazne datoteke i mape, više verzija istog filma ili epizode.
- **Backup i restore** (kartica *Backup i restore*): **puni backup** (baza, postavke, korisnici, pluginovi i statistika gledanja) i **korisnički backup** (gledanost, pozicija, favoriti, ocjene, playliste i postavke korisnika). Oba se vraćaju na isti ili potpuno drugi Jellyfin server (vidi [Backup i restore](#backup-i-restore)).
- **Migracija Jellyfina** (kartica *Migracija*): premješta Jellyfin iz Docker kontejnera u novi Proxmox LXC sa samostalnim Jellyfinom, NVENC/QSV transkodiranjem i NFS medijima. Čarobnjak radi na Proxmox hostu, a pitanja se odgovaraju u sučelju JellyCleana. JellyClean na Proxmoxu nema shell (vidi [Migracija Jellyfina](#migracija-jellyfina-docker--proxmox-lxc)).
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
| `AUTH_USER` / `AUTH_PASS` | | Početni podaci za prijavu. **Postavi ih**, jer sučelje može brisati datoteke. Lozinka se mijenja u sučelju (vidi *Prijava*). |
| `PORT` | `8095` | |
| `JF_SSH_HOST` | `192.168.1.20` | Računalo na kojem radi Jellyfin (npr. VM s Dockerom), za čišćenje config mape |
| `JF_SSH_USER` / `JF_SSH_PORT` | `root` / `22` | SSH korisnik i port |
| `JF_SSH_KEY` | `/var/lib/jellyclean/ssh/id_ed25519` | Privatni ključ (zadano `DATA_DIR/ssh/id_ed25519`) |
| `JF_SSH_SUDO` | `false` | `true` ako SSH korisnik ima **potpuni** sudo bez lozinke (ne preporučuje se) |
| `JF_SSH_HELPER` | `/usr/local/sbin/jellyclean-helper` | Ograničeni pomoćni program na Jellyfin računalu, pokreće se sa `sudo -n` (postavlja ga LXC skripta) |
| `JF_CONFIG_PATH` | `/opt/jellyfin/config` | Jellyfin `/config` mapa na tom računalu |
| `JF_CACHE_PATH`, `JF_LOG_PATH`, `JF_DATA_PATH`, `JF_METADATA_PATH` | | Ako se razlikuju od zadanih (`config/cache`, `config/log`, `config/data`, `data/metadata`). Kod službenog Docker imagea su cache i data drugdje; LXC skripta to otkriva sama |
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

Instalacija radi u **whiptail dijaloškim prozorima**, kao Proxmox helper skripte. Na vrhu ekrana stoji *Korak x/7*, a na početku ASCII logo. Dulji poslovi (kreiranje kontejnera, paketi, otkrivanje) ispisuju napredak u konzoli. *Odustani* uvijek pita prekidaš li instalaciju ili nastavljaš.
Bez prozora, sve u konzoli: `NO_WHIPTAIL=1 bash create-lxc.sh`.

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

**Jellyfin config preko SSH-a** (opcionalno):
- u kontejneru se napravi SSH ključ samo za JellyClean
- ključ se postavi na Jellyfin računalo: upišeš lozinku jednom (`ssh-copy-id`) ili ga dodaš ručno
- putanje se otkrivaju same, preko `docker inspect` za kontejner s „jellyfin” u imenu (linuxserver ili službeni image) ili kao standardna paketna instalacija (`/var/lib/jellyfin`)
- **SSH korisnik ne mora biti root.** Skripta prvo provjeri ima li korisnik već prava na cache, log i metadata mape, što je čest slučaj kod linuxserver imagea s `PUID` jednakim UID-u korisnika. Ako ima, ništa se ne mijenja.
- Ako nema, a root se dobiva preko `sudo` uz lozinku, skripta lozinku pita **jednom** i ne sprema je. Koristi je za otkrivanje putanja i za postavljanje **ograničenog pomoćnog programa**:
  - `/usr/local/sbin/jellyclean-helper`, vlasnik root. Zna samo izmjeriti mape te obrisati cache, stare logove i siročad u metadata, i to samo u putanjama upisanima u njega. Uz to pokreće test enkodiranja i NVENC patch: patch skripta stiže preko SSH-a, ali se pokreće samo ako se njen SHA256 nalazi na popisu odobrenih verzija upisanom u pomoćni program
  - `/etc/sudoers.d/jellyclean`, provjeren s `visudo`: `<korisnik> ALL=(root) NOPASSWD: /usr/local/sbin/jellyclean-helper`

  Tako JellyClean i njegov SSH ključ nemaju potpuni root na tom računalu. Uklanjanje: `sudo rm /usr/local/sbin/jellyclean-helper /etc/sudoers.d/jellyclean`

Kad nema ograničenja ovlasti, na Jellyfin računalu se ništa ne instalira.

Ručno se upisuju samo IP adresa NAS-a, Jellyfin URL i API ključ te, za čišćenje configa, adresa Jellyfin računala. Sve ostalo ima prijedlog.

Ako se NFS export ne montira, najčešći uzrok je da NAS ne dopušta IP adresu kontejnera. Dodaj je u NFS dozvole na NAS-u.

Upravljanje kontejnerom:

```bash
pct exec <ID> -- nano /etc/jellyclean.env      # konfiguracija, pa: systemctl restart jellyclean
pct exec <ID> -- journalctl -u jellyclean -f   # logovi
pct exec <ID> -- /usr/local/bin/jellyclean-update   # nadogradnja (privatni repo: dodaj env GH_TOKEN=xxx ispred)
```

### Nadogradnja postojećeg kontejnera (s Proxmox hosta)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/update-lxc.sh)"
```

Skripta sama pronađe JellyClean kontejnere (po tagu ili hostnameu) ili ih prima kao argumente (`update-lxc.sh 105`). U svaki postavi ili osvježi `/usr/local/bin/jellyclean-update` i pokrene ga. Nadogradnja radi i za instalaciju iz gita (`git pull`) i bez gita (preuzme se najnoviji release). Za privatni repozitorij: `GH_TOKEN=xxx bash update-lxc.sh`.

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

## NVENC patch drivera

Kartica *Test enkodiranja* → **Ograničenje NVENC sesija**:

- pokazuje verziju drivera na Jellyfin računalu, je li driver patchan i postoji li patch za tu verziju
- **Primijeni patch**: zaustavi Jellyfin, spremi original biblioteke (`/opt/nvidia/libnvidia-encode-backup`), primijeni patch, ponovno pokrene Jellyfin i provjeri 13 istovremenih sesija
- **Vrati original**: vraća sigurnosnu kopiju
- **Provjeri ograničenje**: 13 kratkih istovremenih NVENC enkodiranja unutar Jellyfin okruženja
- nakon nadogradnje drivera javlja da je patch nestao

Koristi se zakucana, provjerena verzija skripte `patch.sh` iz keylase/nvidia-patch (commit u `NVPATCH_COMMIT`). JellyClean je preuzima s GitHuba i provjerava SHA256, a pomoćni program na Jellyfin računalu provjerava ga ponovno prije pokretanja. Ako LXC nema pristup GitHubu, datoteku možeš ručno spremiti u `DATA_DIR/nvidia-patch.sh`.

Patch se primjenjuje na računalu na kojem je instaliran NVIDIA driver (VM ili host s Dockerom). Ne radi kad je Jellyfin u LXC-u kojem driver dolazi s Proxmox hosta. Patch je neslužben i protivan NVIDIA licenci za GeForce kartice. Traži `ALLOW_DELETE=true`.

## Više Jellyfin servera

JellyClean može upravljati s više Jellyfin servera koji koriste **iste medije** (isti NAS):

- **Glavni server** dolazi iz `/etc/jellyclean.env`, kako ga postavi instalacijska skripta.
- **Ostale servere** dodaješ u sučelju, na kartici **Serveri → + Dodaj server**: naziv, URL, API ključ, PATH_MAP te SSH host i korisnik.
- **Postavi pristup** daje jednu naredbu (`curl … | sudo sh`) koju pokreneš na računalu s Jellyfinom. Naredba dopusti JellyCleanovom SSH ključu prijavu, pronađe Jellyfin (Docker ili paket) i postavi pomoćni program sa sudo pravilom samo za njega. Poveznica vrijedi 30 minuta, a sudo lozinka se nigdje ne sprema. Ista naredba služi i za nadogradnju pomoćnog programa na tom računalu.
- **Izbornik u zaglavlju** bira server za kartice Jellyfin, Provjera putanja, Jellyfin config i Test enkodiranja (uključujući nadzor kartice i NVENC patch).
- Kartice Pregled, Preglednik, Najveće datoteke, Smeće i duplikati i Kanta su zajedničke, jer su mediji isti.
- Nakon brisanja ili vraćanja datoteka JellyClean javlja promjenu **svim** serverima. Automatska sinkronizacija također obuhvaća sve servere.

Postavke servera spremaju se u `DATA_DIR/servers.json` (prava 600). Stanje po serveru, kao rezultati testa enkodiranja i NVENC patcha, sprema se u `DATA_DIR/servers/<id>/`. Izmjene glavnog servera napravljene u sučelju imaju prednost pred env datotekom, a gumb **Vrati iz env** ih poništava.

## Migracija Jellyfina (Docker → Proxmox LXC)

Kartica **Migracija** premješta Jellyfin iz Docker kontejnera (npr. u VM-u na starom serveru) u novi LXC na Proxmox hostu, sa samostalnim Jellyfinom iz službenog repozitorija:

- prenosi sve postavke, korisnike, bazu, gledanost, metapodatke i pluginove
- hardversko transkodiranje: NVIDIA NVENC/NVDEC (drajver na hostu, iste knjižnice u LXC-u) ili Intel Quick Sync
- LXC na odabranom storageu (zadano NVMe LVM-Thin), mediji s NAS-a preko NFS-a
- **provjera putanja biblioteka**: prije kreiranja LXC-a usporedi putanje iz Jellyfin baze s NFS shareom i sama predloži ispravnu točku montiranja (npr. `/nas/Gondolin` umjesto `/nas`)
- transkodiranje uvijek lokalno; putanje koje su postojale samo u Dockeru se otkriju i poprave, a pri pokretanju Jellyfina ispravljaju se i greške *Access denied*
- opcija da stari i novi Jellyfin rade paralelno (novi Server ID i ime)
- svaki odgovor se pamti, pa nakon greške ne treba sve upisivati ponovno

### Pristup Proxmoxu: ograničeni ključ

Migracija mora raditi na Proxmox hostu kao root (`pct`, storage, GPU), ali JellyClean **ne dobiva shell**:

1. **Postavi pristup** → upiši IP Proxmoxa. JellyClean napravi **novi SSH ključ samo za Proxmox** i da naredbu za shell Proxmoxa (web sučelje → node → *Shell*). Naredba prvo provjeri SHA256 skripte, a poveznica vrijedi 30 minuta.
2. Naredba instalira `/usr/local/sbin/jellyclean-pve-agent` i `/usr/local/sbin/jellyfin-docker2lxc` te u `authorized_keys` roota (na Proxmoxu `/etc/pve/priv/authorized_keys`) upiše:
   ```
   from="<IP JellyCleana>",command="/usr/local/sbin/jellyclean-pve-agent <ID>",restrict ssh-ed25519 AAAA… jellyclean-pve <ID>
   ```
   - `command=`: što god klijent pošalje, sshd pokreće **samo agenta**. Agent zna samo `ping`, `start`, `status`, `answer`, `cancel`, `log` i `revoke`, a sve ostalo odbija.
   - `from=`: ključ radi **samo s IP adrese JellyCleana**
   - `restrict`: bez terminala, prosljeđivanja portova, agenta i X11
3. **Pokrenuo sam - provjeri ključ** aktivira novi ključ.

**Provjera ključa** (gumb *Provjeri ključ*, automatski svakih 30 minuta i pri otvaranju kartice):
- privatni ključ postoji, ima prava 600 (kriva prava se ispravljaju) i odgovara javnom
- agent potvrđuje da je prijava stigla **baš tim ključem** (ID ključa iz `command=`)
- verzije agenta i migracijske skripte na Proxmoxu odgovaraju ovoj verziji JellyCleana. Ako ne odgovaraju, nudi se *Ažuriraj na Proxmoxu*
- trenutna IP adresa JellyCleana uspoređuje se s dopuštenom

**Promjena IP adrese JellyCleana**: Proxmox odbija ključ (`from=`) i kartica javlja *IP adresa promijenjena*. **Novo dopuštenje** napravi novi ključ za novu adresu i daje novu naredbu. Ta naredba na Proxmoxu **briše sve stare ključeve ovog JellyCleana**, a kad novi ključ proradi, JellyClean briše i svoju kopiju starog. Isto vrijedi za *Zamijeni ključ* i *Ažuriraj na Proxmoxu*.

**Ukloni pristup** briše ključ na Proxmoxu i u JellyCleanu. Ako Proxmox tada nije dostupan, sučelje pokaže naredbu za ručno uklanjanje.

Ključ je u `DATA_DIR/ssh/pve-<ID>`, a stanje u `DATA_DIR/pve.json` (prava 600). Lozinke za Docker VM (SSH i sudo) upisuješ u sučelju. Prolaze kroz agenta do skripte, koja ih koristi samo dok radi i nigdje ih ne zapisuje.

### Bez JellyCleana

Ista skripta radi i samostalno na Proxmox hostu, s whiptail čarobnjakom:

```bash
bash proxmox/jellyfin-docker2lxc.sh            # nova migracija ili nastavak
bash proxmox/jellyfin-docker2lxc.sh --resume   # nastavak nakon restarta hosta
bash proxmox/jellyfin-docker2lxc.sh --text     # bez prozora
bash proxmox/jellyfin-docker2lxc.sh --sync-nvidia <ID>   # nakon nadogradnje NVIDIA drajvera na hostu
```

## Backup i restore

Kartica **Backup i restore** radi dvije vrste sigurnosnih kopija. Spremaju se **lokalno** (`DATA_DIR/backups` u JellyCleanu) ili na **NAS preko NFS-a** (vidi [Pohrana na NAS](#pohrana-na-nas-nfs)). Mogu se preuzeti kao `.tar` i učitati u drugi JellyClean.

### Puni backup (SSH)

Sprema **sve** iz Jellyfinove mape: bazu (`jellyfin.db`), postavke (`*.xml`), korisnike i lozinke, ključeve uređaja, pluginove i njihove postavke, biblioteke (`root/`) i **statistiku gledanja** (`playback_reporting.db` plugina Playback Reporting). Po želji sprema i metapodatke i slike. Cache, logovi i privremene datoteke transkodiranja se ne spremaju.

- Jellyfin se tijekom pakiranja **zaustavi** (preporučeno), a zatim ponovno pokrene.
- **Bez zaustavljanja**: SQLite baze (`jellyfin.db`, `playback_reporting.db`, baze pluginova) ne kopiraju se izravno, nego preko SQLite online backupa (`python3` ili `sqlite3` na Jellyfin računalu) s provjerom `quick_check`. Kopija je dosljedna iako Jellyfin za to vrijeme piše u bazu. Ako backup jedne baze ne uspije, ona se kopira izravno i javlja se upozorenje.
- **Vraćanje na isti server**: Jellyfin se zaustavi, trenutni podaci se premjeste u `.jellyclean-restore-<vrijeme>/prev` u Jellyfinovoj mapi, kopija se raspakira, vlasnik datoteka se postavi kao prije i Jellyfin se pokrene. Stari podaci ostaju dok ih ne obrišeš (*Podaci prije vraćanja → Obriši sve*).
- **Vraćanje na drugi server** (prvo ga dodaj na kartici *Serveri*): isto, a uz to:
  - putanje u postavkama i pluginovima prilagode se odredištu (npr. Docker `/config/data` → `/var/lib/jellyfin`), prema `/System/Info` obaju servera
  - po želji **novi Server ID**, ako stari server i dalje radi
  - provjerava se da odredišni Jellyfin nije stariji od izvornog
  - ako su mediji na drugim putanjama, nakon vraćanja otvori **Provjera putanja**

Treba SSH pristup Jellyfin računalu. Pomoćni program je nadograđen na verziju 11, pa na postojećim serverima jednom ponovno pokreni *Serveri → Postavi pristup*.

### Korisnički backup (Jellyfin API)

Za svakog odabranog korisnika sprema: odgledano, broj gledanja, datum zadnjeg gledanja, gdje je stao, favorite, ocjene (sviđa mi se / ne sviđa), playliste, osobne postavke (jezik zvuka i titlova, početna stranica…) i dozvole.

Vraća se na **bilo koji** Jellyfin, pa i na potpuno novi server s drugim ID-ovima i putanjama. Stavke se prepoznaju redom po:
1. istom ID-u (isti server)
2. istoj putanji (ili zamjeni putanja koju upišeš, npr. `/nas/Gondolin => /mnt/media`)
3. IMDb / TMDb / TVDb oznakama
4. za epizode: oznakama ili nazivu serije te broju sezone i epizode
5. nazivu i godini

Korisnici se povezuju po imenu, a nepostojeći se po želji stvore s nasumičnom lozinkom (prikaže se na kraju). Biblioteke u postavkama i dozvolama povezuju se po nazivu. Ponovno vraćanje ne duplicira playliste. Na kraju se po korisniku prikaže koliko je stavki prepoznato, a koje nisu pronađene.

Statistika gledanja (Playback Reporting) nije dio korisničkog backupa, jer ide samo uz cijelu bazu. Za nju koristi puni backup.

### Pohrana na NAS (NFS)

U kartici **Backup i restore → Mjesta pohrane → Dodaj NAS (NFS)**:

1. Upiši IP NAS-a → **Pronađi exporte** (ili upiši export ručno, npr. `192.168.8.20:/mnt/user/Backup`).
2. Odaberi verziju NFS-a, mapu na NAS-u (zadano `jellyclean`) i naziv → **Testiraj** → **Dodaj i montiraj**.

NAS se montira **unutar kontejnera JellyCleana** u `/mnt/jellyclean-nas/<id>` i upisuje u `/etc/fstab` kontejnera, pa ostaje i nakon restarta. Ništa se ne dodaje u Proxmox storage. Kontejner mora biti privilegiran sa značajkom `mount=nfs`, kako ga radi `create-lxc.sh`.

- Kod izrade kopije biraš **Spremi na**: lokalno ili neki od NAS-ova. Odabir se pamti.
- Postojeću kopiju možeš **kopirati ili premjestiti** između lokalne pohrane i NAS-a (gumb **Na…**). Premještanje briše izvor tek nakon provjere veličine.
- Vraćanje radi izravno s NAS-a.
- Za svaki NAS vidi se zauzeće i **provjerava se može li JellyClean pisati** u mapu. Ako ne može, na NAS-u dopusti pisanje za IP kontejnera (pravo rw, bez `root_squash` ili s mapiranjem na vlasnika mape).
- **Odspoji / Montiraj / Ukloni**: uklanjanje odmontira NAS i briše ga iz `fstab`-a, a kopije na NAS-u ostaju.

Montiranje radi root pomoćni program `/usr/local/sbin/jellyclean-nas`. Servis JellyCleana smije ga pokretati preko `sudo` (pravilo u `/etc/sudoers.d/jellyclean-nas`). Program upravlja samo NFS mountovima ispod `/mnt/jellyclean-nas` i provjerava svaki ulaz. Instalira se automatski pri instalaciji i nadogradnji (`update-lxc.sh`). Ručno, unutar kontejnera:

```bash
bash /opt/jellyclean/proxmox/lxc-post-update.sh
```

## Prijava

- Sučelje ima vlastitu stranicu za prijavu (nema više popupa preglednika). Opcija *Zapamti me* drži prijavu 30 dana, inače 12 sati.
- Sesija je potpisani `HttpOnly` kolačić (`SameSite=Strict`, iza HTTPS proxyja i `Secure`). Vrijedi i nakon restarta i ažuriranja.
- Nakon 5 pogrešnih pokušaja s iste adrese prijava se privremeno zaključava, sa sve duljim čekanjem (do 15 min).
- **Promjena lozinke i korisničkog imena**: gumb s imenom u gornjem desnom kutu → *Promijeni lozinku*. Nova lozinka sprema se u `/var/lib/jellyclean/auth.json` (PBKDF2-SHA256, prava 600) i od tada ima prednost pred `AUTH_USER`/`AUTH_PASS`. Sve ostale prijave se odjavljuju.
- **Zaboravljena lozinka**: s Proxmox hosta `pct exec <ID> -- rm /var/lib/jellyclean/auth.json && pct exec <ID> -- systemctl restart jellyclean`. Tada ponovno vrijedi lozinka iz `/etc/jellyclean.env`.
- Skripte i `curl` i dalje mogu koristiti Basic auth (`curl -u korisnik:lozinka …`).

