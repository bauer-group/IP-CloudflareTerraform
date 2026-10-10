# Changelog

All notable changes to this project are documented here. This file is maintained
automatically by [semantic-release](https://github.com/semantic-release/semantic-release)
on every release to `main`.

## [0.2.3](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.2.2...v0.2.3) (2026-10-10)

### 🔧 Maintenance

* **deps:** update base image backuphelper [skip ci] ([dca4ca8](https://github.com/bauer-group/IP-CloudflareTerraform/commit/dca4ca859d27a27c5f16d56ecdc336bde89201b9))
* synced Dockerfile versions to 0.2.2 [skip ci] ([4df2f54](https://github.com/bauer-group/IP-CloudflareTerraform/commit/4df2f54317336715efa996ea202f5a92c56d1d27))

## [0.2.2](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.2.1...v0.2.2) (2026-10-09)

### 🐛 Bug Fixes

* **cf-backup:** compared only the selected zone in drift --zone ([781688d](https://github.com/bauer-group/IP-CloudflareTerraform/commit/781688d66ac36de64b2e929e3e010214ecc62568))
* **cf-backup:** dropped the empty load balancer monitor header ([01cceda](https://github.com/bauer-group/IP-CloudflareTerraform/commit/01cceda73f949b68fe2dfea58115ae4e2367c222))
* **cf-backup:** exported each curated type at the scope of its API ([87fa43a](https://github.com/bauer-group/IP-CloudflareTerraform/commit/87fa43af013e0c3e337f1b4c54bd8b2297d89b61))
* **cf-backup:** kept import files the rewrites cannot fully read ([d0bca87](https://github.com/bauer-group/IP-CloudflareTerraform/commit/d0bca877113f5e617d07e1cf8b4f857b42e9b01b))
* **cf-backup:** kept the Host header of load balancer pool origins ([ce8c599](https://github.com/bauer-group/IP-CloudflareTerraform/commit/ce8c5998796c475531015beb17f39bb054f7c58d))
* **cf-backup:** kept the originRequest settings of tunnel configs ([ae14738](https://github.com/bauer-group/IP-CloudflareTerraform/commit/ae14738b7a1437168f65bdcf40abcf32085a013e))
* **cf-backup:** pointed import blocks at the generated resources ([780bd52](https://github.com/bauer-group/IP-CloudflareTerraform/commit/780bd52afe199fbabc164c98fd0dc939c512c845))
* **cf-backup:** removed no-op restore changes for two zone types ([8ed3ec6](https://github.com/bauer-group/IP-CloudflareTerraform/commit/8ed3ec68305ed6aeab658f8e346e5769bebcc83b))
* **cf-backup:** replaced import ids that name the scope twice ([9df9fc6](https://github.com/bauer-group/IP-CloudflareTerraform/commit/9df9fc668f021519b859e3d0bc6a44661714b749))
* **cf-backup:** routed cf-terraforming's legacy client to api_base ([1d8c130](https://github.com/bauer-group/IP-CloudflareTerraform/commit/1d8c130c9d477d07be0f1e303948852d6263761f))
* **cf-backup:** stopped drift --zone on a zone the token cannot see ([59413ad](https://github.com/bauer-group/IP-CloudflareTerraform/commit/59413adfa40fbdd5e0de9d5a749140f87666c151))
* **cf-backup:** stopped exporting the non-functional snippets type ([540425e](https://github.com/bauer-group/IP-CloudflareTerraform/commit/540425e20bb9d0722586accb039a0ea0b464de38))

### 🔧 Maintenance

* synced Dockerfile versions to 0.2.1 [skip ci] ([a274650](https://github.com/bauer-group/IP-CloudflareTerraform/commit/a274650d0336f6f14c916cead8d758bee4de5209))

## [0.2.1](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.2.0...v0.2.1) (2026-10-09)

### 🔧 Maintenance

* **deps:** update base image backuphelper [skip ci] ([c8b3f8a](https://github.com/bauer-group/IP-CloudflareTerraform/commit/c8b3f8a106d9cd0d4679c4df1ce331d30b6742ec))
* synced Dockerfile versions to 0.2.0 [skip ci] ([19da200](https://github.com/bauer-group/IP-CloudflareTerraform/commit/19da20018b4f360490ee597e28606fc627848f6f))

## [0.2.0](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.7...v0.2.0) (2026-10-09)

### 🚀 Features

* **compose:** exposed resource types and the API base URL in .env ([eb48aa6](https://github.com/bauer-group/IP-CloudflareTerraform/commit/eb48aa6965ae8e543a16b50dfa59da232ce50fb1))

### 🐛 Bug Fixes

* **cf-backup:** pointed cf-terraforming and OpenTofu at api_base ([f5fe530](https://github.com/bauer-group/IP-CloudflareTerraform/commit/f5fe530128e1b137314e7fc3ee3da44a9158302e))

### 🔧 Maintenance

* synced Dockerfile versions to 0.1.7 [skip ci] ([db03698](https://github.com/bauer-group/IP-CloudflareTerraform/commit/db03698869d3538dbbf72402ad2e289f97299f7a))

## [0.1.7](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.6...v0.1.7) (2026-10-08)

### 🔧 Maintenance

* **deps:** update base image backuphelper [skip ci] ([92f413a](https://github.com/bauer-group/IP-CloudflareTerraform/commit/92f413afd825bcb96c2cc52965917e570c8d9769))
* synced Dockerfile versions to 0.1.6 [skip ci] ([1be6a49](https://github.com/bauer-group/IP-CloudflareTerraform/commit/1be6a491199827c608f25da026ac4c5a5e85307c))

## [0.1.6](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.5...v0.1.6) (2026-10-07)

### 🔧 Maintenance

* **deps:** update base image backuphelper ([7ef1eeb](https://github.com/bauer-group/IP-CloudflareTerraform/commit/7ef1eeb9be5492fe8b41f790b4a07fd20711de3e))
* synced Dockerfile versions to 0.1.5 [skip ci] ([cf4fd38](https://github.com/bauer-group/IP-CloudflareTerraform/commit/cf4fd38a42ae81c3295f22b737977fecf5231af9))

## [0.1.5](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.4...v0.1.5) (2026-10-03)

### 🔧 Maintenance

* **ci:** removed issue AI summary workflow ([2ad25f9](https://github.com/bauer-group/IP-CloudflareTerraform/commit/2ad25f95171bad7315549fa21b517504d9c2aeac)), references [bauer-group/automation-templates#105](https://github.com/bauer-group/automation-templates/issues/105)
* **deps:** update base image backuphelper ([de28e4e](https://github.com/bauer-group/IP-CloudflareTerraform/commit/de28e4ee39da970adcb2f68da9173719794d6231))
* synced Dockerfile versions to 0.1.4 [skip ci] ([977efc0](https://github.com/bauer-group/IP-CloudflareTerraform/commit/977efc0d266b15bd60581e7a98c1b8be8459aa4c))

## [0.1.4](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.3...v0.1.4) (2026-09-19)

### 🔧 Maintenance

* **deps:** update base image backuphelper ([1a375b1](https://github.com/bauer-group/IP-CloudflareTerraform/commit/1a375b10c90b29ab01346096a0dfbb00dbf622e5))
* synced Dockerfile versions to 0.1.3 [skip ci] ([93c2708](https://github.com/bauer-group/IP-CloudflareTerraform/commit/93c27084f272137347c185c4f9c58188fa011b5f))

## [0.1.3](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.2...v0.1.3) (2026-09-02)

## [0.1.2](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.1...v0.1.2) (2026-08-07)

### 🐛 Bug Fixes

* **ci:** added the missing permissions block ([9d80071](https://github.com/bauer-group/IP-CloudflareTerraform/commit/9d80071c8b73a11ada401e43501d4bf79610b423))

## [0.1.1](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.1.0...v0.1.1) (2026-07-17)

## [0.1.0](https://github.com/bauer-group/IP-CloudflareTerraform/compare/v0.0.0...v0.1.0) (2026-07-17)

### 🚀 Features

* **cf-backup:** added Cloudflare backup toolkit ([75fda51](https://github.com/bauer-group/IP-CloudflareTerraform/commit/75fda514519a8609d7a1cfd64d5ccac03c586b3e))
* **cf-backup:** added max-coverage schema sweep ([bcb3cda](https://github.com/bauer-group/IP-CloudflareTerraform/commit/bcb3cdab828e06c3939e1ebc4c03a297d8f02e50))
* **cf-backup:** added tunnel config export ([9e4fbb6](https://github.com/bauer-group/IP-CloudflareTerraform/commit/9e4fbb67171b5edd1bf9e4f4d08f08597babc66f))
* **cf-backup:** added zone-settings export ([f8df36c](https://github.com/bauer-group/IP-CloudflareTerraform/commit/f8df36c4318ef39851d71cf7c9e09d44196e5f15))
