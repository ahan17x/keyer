# Area estimates with Yosys

`synth.ys` synthesises the design against the IHP CMOS5L typical liberty;
`run_sram.ys` additionally black-boxes the SRAM macro so the number is the
logic alone. Set `LIB` to the liberty file from the IHP Open PDK `dev` branch:

```sh
git clone --depth 1 --filter=blob:none --sparse -b dev https://github.com/IHP-GmbH/IHP-Open-PDK.git ihp-pdk
cd ihp-pdk && git sparse-checkout set --no-cone '/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/*' && cd ..
LIB=$PWD/ihp-pdk/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/sg13cmos5l_stdcell_typ_1p20V_25C.lib
cd synth && sed "s|LIB|$LIB|g" synth.ys > run.ys && yosys -q -l synth.log run.ys
```

`core_only.ys` synthesises `keyer_core` alone with the thread count
substituted for `@N@` (`sed "s|LIB|$LIB|g; s|@N@|4|" core_only.ys > run_core4.ys`).
The generated `run*.ys` files are ignored by git; the three sources are not.

Results are logged in `docs/AREA.md`.
