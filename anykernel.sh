### AnyKernel3 Ramdisk Mod Script
## onclite-oc

### AnyKernel setup
properties() { '
kernel.string=onclite-oc (SDM632 CPU/GPU overclock)
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
device.name1=onclite
device.name2=onc
device.name3=Redmi 7
device.name4=Redmi Y3
'; }

block=/dev/block/bootdevice/by-name/boot;
is_slot_device=0;
ramdisk_compression=auto;

### AnyKernel install
. tools/ak3-core.sh;

split_boot;
flash_boot;
