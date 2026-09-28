# Notices for the SRA Toolkit binaries in the optional `sra` image

The `sra` build target copies three binaries from NCBI's `sratoolkit.3.4.1-ubuntu64.tar.gz` (SHA-256 `b950362c054765a4184af41947f022f040e94e964862017c0ecb0b0273db3596`): `sratools.3.4.1`, `prefetch-orig.3.4.1` and `fasterq-dump-orig.3.4.1`, plus NCBI's `bin/ncbi` configuration. They are built from:

- sra-tools tag `3.4.1`, commit `ded4303eb477047590b219f6a2e8397b12d58cc0` (https://github.com/ncbi/sra-tools)
- ncbi-vdb tag `3.4.1`, commit `28118ec6254c1a1acbf47ecbdc775d693f178573` (https://github.com/ncbi/ncbi-vdb)

NCBI's own code is a United States Government work in the public domain. The binaries also statically include third-party code under other licences. Each of the three binaries was checked for these components (unstripped symbol names and version strings); all three contain all five. The texts below are copied unchanged from the pinned sources; the file SHA-256 is given for each.

| Component | Version | Licence | Text in this directory | Copied from | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| sra-tools (NCBI) | 3.4.1 | Public domain notice | `sra-tools-3.4.1-LICENSE` | sra-tools `LICENSE` | `122edf134e4230505179e9a1891ac3fea334f1b67d767933e9e86dc5b49adfdb` |
| ncbi-vdb (NCBI) | 3.4.1 | Public domain notice, lists the exceptions below | `ncbi-vdb-3.4.1-LICENSE` | ncbi-vdb `LICENSE` | `47b01dce02a30bfeec71faaa9238bad78af1c5bc3882b985c21ea212474f7793` |
| bzip2/libbzip2 | 1.0.8 | bzip2 licence (BSD-style) | `bzip2-1.0.8-LICENSE` | ncbi-vdb `libs/ext/bzip2/LICENSE` | `c6dbbf828498be844a89eaa3b84adbab3199e342eb5cb2ed2f0d4ba7ec0f38a3` |
| zlib | 1.3.1 | zlib licence | `zlib-1.3.1-LICENSE` | ncbi-vdb `libs/ext/zlib/LICENSE` | `845efc77857d485d91fb3e0b884aaa929368c717ae8186b66fe1ed2495753243` |
| Zstandard | 1.5.7 | BSD-3-Clause, or GPL-2.0 at the recipient's choice | `zstd-1.5.7-LICENSE`, `zstd-1.5.7-COPYING` | ncbi-vdb `libs/ext/zstd/LICENSE`, `COPYING` | `7055266497633c9025b777c78eb7235af13922117480ed5c674677adc381c9d8`, `f9c375a1be4a41f7b70301dd83c91cb89e41567478859b77eef375a52d782505` |
| Mbed TLS (symbols renamed `vdb_mbedtls_`) | 3.2.1 | Apache-2.0 | `mbedtls-3.2.1-LICENSE`, `mbedtls-3.2.1-apache-2.0.txt` | ncbi-vdb `libs/ext/mbedtls/LICENSE`; Apache text from Mbed TLS tag `v3.2.1` (commit `869298bffeea13b205343361b7a7daf2b210e33d`) `LICENSE`, because the pinned ncbi-vdb tree does not include the `apache-2.0.txt` its `LICENSE` names | `d4412d97a34e5dd3263394d02f14be1c4a99794ef66669a3a8b4b87dce31aa9f`, `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `ksort` in ncbi-vdb `libs/klib/qsort.c` (from the GNU C Library, by Douglas C. Schmidt) | ncbi-vdb 3.4.1 | LGPL-2.1-or-later | `lgpl-2.1.txt` (from https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt, which the pinned trees do not include); corresponding source `ncbi-vdb-3.4.1-libs-klib-qsort.c` | ncbi-vdb `libs/klib/qsort.c` | `20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95`, `152a1bccd9008fe965db27c90b96fe4da80a74ccc3312955c3395d1bbfcd69b6` |

The complete source of the binaries, needed to modify and relink the LGPL component, is public at the two commits above. ncbi-vdb also vendors `regex` and `szip`; neither was found in these three binaries. The binaries link dynamically only to glibc, which comes from the Debian base image with its own notices under `/usr/share/doc`.

This inventory records the components and their texts; it is not legal advice.
