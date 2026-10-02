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

Results so far (2026-10-01): flop memory 18,956 cells / 429,092 um^2; with the
macro black-boxed 6,410 cells, 1,190 flops, 114,169 um^2 (+28,127 um^2 macro).
