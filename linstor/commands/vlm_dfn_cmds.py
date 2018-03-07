import linstor
from linstor.commands import Commands, ResourceDefinitionCommands
from linstor.utils import SizeCalc, approximate_size_string, namecheck, Output
from linstor.consts import RES_NAME, Color, ExitCode
from linstor.sharedconsts import (
    FLAG_DELETE
)

import re
import sys


class VolumeDefinitionCommands(Commands):
    def __init__(self):
        super(VolumeDefinitionCommands, self).__init__()

    def setup_commands(self, parser):
        p_new_vol = parser.add_parser(
            Commands.CREATE_VOLUME_DEF,
            aliases=['crtvlmdfn'],
            description='Defines a volume with a capacity of size for use with '
            'linstore. If the resource resname exists already, a new volume is '
            'added to that resource, otherwise the resource is created automatically '
            'with default settings. Unless minornr is specified, a minor number for '
            "the volume's DRBD block device is assigned automatically by the "
            'linstor server.')
        p_new_vol.add_argument('-n', '--vlmnr', type=int)
        p_new_vol.add_argument('-m', '--minor', type=int)
        p_new_vol.add_argument('-d', '--deploy', type=int)
        p_new_vol.add_argument('-s', '--site', default='',
                               help="only consider nodes from this site")
        p_new_vol.add_argument('resource_name', type=namecheck(RES_NAME),
                               help='Name of an existing resource').completer = ResourceDefinitionCommands.completer
        p_new_vol.add_argument(
            'size',
            help='Size of the volume in resource. '
            'The default unit for size is GiB (size * (2 ^ 30) bytes). '
            'Another unit can be specified by using an according postfix. '
            "Linstor's internal granularity for the capacity of volumes is one "
            'Kibibyte (2 ^ 10 bytes). All other unit specifications are implicitly '
            'converted to Kibibyte, so that the actual size value used by linstor '
            'is the smallest natural number of Kibibytes that is large enough to '
            'accommodate a volume of the requested size in the specified size unit.'
        ).completer = VolumeDefinitionCommands.size_completer
        p_new_vol.set_defaults(func=self.create)

        # remove-volume definition
        p_rm_vol = parser.add_parser(
            Commands.DELETE_VOLUME_DEF,
            aliases=['delvlmdfn'],
            description='Removes a volume definition from the linstor cluster, and removes '
            'the volume definition from the resource definition. The volume is '
            'undeployed from all nodes and the volume entry is marked for removal '
            "from the resource definition in linstor's data tables. After all "
            'nodes have undeployed the volume, the volume entry is removed from '
            'the resource definition.')
        p_rm_vol.add_argument('-q', '--quiet', action="store_true",
                              help='Unless this option is used, linstor will issue a safety question '
                              'that must be answered with yes, otherwise the operation is canceled.')
        p_rm_vol.add_argument('resource_name',
                              help='Resource name of the volume definition'
                              ).completer = ResourceDefinitionCommands.completer
        p_rm_vol.add_argument(
            'volume_nr',
            type=int,
            help="Volume number to delete.")
        p_rm_vol.set_defaults(func=self.delete)

        # list volume definitions
        resgroupby = ()
        volgroupby = resgroupby + ('Vol_ID', 'Size', 'Minor')
        vol_group_completer = Commands.show_group_completer(volgroupby, 'groupby')

        p_lvols = parser.add_parser(
            Commands.LIST_VOLUME_DEF,
            aliases=['dspvlmdfn'],
            description=' Prints a list of all volume definitions known to linstor. '
            'By default, the list is printed as a human readable table.')
        p_lvols.add_argument('-p', '--pastable', action="store_true", help='Generate pastable output')
        p_lvols.add_argument('-g', '--groupby', nargs='+',
                             choices=volgroupby).completer = vol_group_completer
        p_lvols.add_argument('-R', '--resources', nargs='+', type=namecheck(RES_NAME),
                             help='Filter by list of resources').completer = ResourceDefinitionCommands.completer
        p_lvols.set_defaults(func=self.list)

        # show properties
        p_sp = parser.add_parser(
            Commands.GET_VOLUME_DEF_PROPS,
            aliases=['dspvlmdfnprp'],
            description="Prints all properties of the given volume definition.")
        p_sp.add_argument(
            'resource_name',
            help="Resource name").completer = ResourceDefinitionCommands.completer
        p_sp.add_argument(
            'volume_nr',
            type=int,
            help="Volume number")
        p_sp.set_defaults(func=self.print_props)

        # set properties
        # disabled until there are properties
        # p_setprop = parser.add_parser(
        #     Commands.SET_VOLUME_DEF_PROP,
        #     aliases=['setvlmdfnprp'],
        #     description='Sets properties for the given volume definition.')
        # p_setprop.add_argument(
        #     'resource_name',
        #     help="Resource name").completer = ResourceDefinitionCommands.completer
        # p_setprop.add_argument(
        #     'volume_nr',
        #     type=int,
        #     help="Volume number")
        # Commands.add_parser_keyvalue(p_setprop, "volume-definition")
        # p_setprop.set_defaults(func=self.set_props)

        # set aux properties
        p_setprop = parser.add_parser(
            Commands.SET_VOLUME_DEF_AUX_PROP,
            aliases=['setvlmdfnauxprp'],
            description='Sets properties for the given volume definition.')
        p_setprop.add_argument(
            'resource_name',
            help="Resource name").completer = ResourceDefinitionCommands.completer
        p_setprop.add_argument(
            'volume_nr',
            type=int,
            help="Volume number")
        Commands.add_parser_keyvalue(p_setprop)
        p_setprop.set_defaults(func=self.set_prop_aux)

    def create(self, args):
        replies = self._linstor.volume_dfn_create(
            args.resource_name,
            self._get_volume_size(args.size),
            args.vlmnr,
            args.minor
        )
        return self.handle_replies(args, replies)

    def delete(self, args):
        replies = self._linstor.volume_dfn_delete(args.resource_name, args.volume_nr)
        return self.handle_replies(args, replies)

    def list(self, args):
        lstmsg = self._linstor.resource_dfn_list()

        if lstmsg:
            if args.machine_readable:
                self._print_machine_readable([lstmsg])
            else:
                tbl = linstor.Table(utf8=not args.no_utf8, colors=not args.no_color, pastable=args.pastable)
                tbl.add_column("ResourceName")
                tbl.add_column("VolumeNr")
                tbl.add_column("VolumeMinor")
                tbl.add_column("Size")
                tbl.add_column("State", color=Output.color(Color.DARKGREEN, args.no_color))
                for rsc_dfn in lstmsg.rsc_dfns:
                    for vlmdfn in rsc_dfn.vlm_dfns:
                        tbl.add_row([
                            rsc_dfn.rsc_name,
                            vlmdfn.vlm_nr,
                            vlmdfn.vlm_minor,
                            approximate_size_string(vlmdfn.vlm_size),
                            tbl.color_cell("DELETING", Color.RED)
                                if FLAG_DELETE in rsc_dfn.rsc_dfn_flags else tbl.color_cell("ok", Color.DARKGREEN)
                        ])
                tbl.show()

        return ExitCode.OK

    @classmethod
    def _get_volume_size(cls, size_str):
        m = re.match('(\d+)(\D*)', size_str)

        size = 0
        try:
            size = int(m.group(1))
        except AttributeError:
            sys.stderr.write('Size is not a valid number\n')
            sys.exit(ExitCode.ARGPARSE_ERROR)

        unit_str = m.group(2)
        if unit_str == "":
            unit_str = "GiB"
        try:
            unit = SizeCalc.UNITS_MAP[unit_str.lower()]
        except KeyError:
            sys.stderr.write('"%s" is not a valid unit!\n' % (unit_str))
            sys.stderr.write('Valid units: %s\n' % (','.join(SizeCalc.UNITS_MAP.keys())))
            sys.exit(ExitCode.ARGPARSE_ERROR)

        unit = SizeCalc.UNITS_MAP[unit_str.lower()]

        if unit != SizeCalc.UNIT_kiB:
            size = SizeCalc.convert_round_up(size, unit,
                                             SizeCalc.UNIT_kiB)

        return size

    @staticmethod
    def size_completer(prefix, **kwargs):
        choices = SizeCalc.UNITS_MAP.keys()
        m = re.match('(\d+)(\D*)', prefix)

        digits = m.group(1)
        unit = m.group(2)

        if unit and unit != "":
            p_units = [x for x in choices if x.startswith(unit)]
        else:
            p_units = choices

        return [digits + u for u in p_units]

    def print_props(self, args):
        lstmsg = self._linstor.resource_dfn_list()

        result = []
        if lstmsg:
            for rsc_dfn in [x for x in lstmsg.rsc_dfns if x.rsc_name == args.resource_name]:
                for vlmdfn in rsc_dfn.vlm_dfns:
                    if vlmdfn.vlm_nr == args.volume_nr:
                        result.append(vlmdfn.vlm_props)
                        break

        Commands._print_props(result, args.machine_readable)
        return ExitCode.OK

    def set_props(self, args):
        mod_prop_dict = Commands.parse_key_value_pairs([args.key + '=' + args.value])
        replies = self._linstor.volume_dfn_modify(
            args.resource_name,
            args.volume_nr,
            mod_prop_dict['pairs'],
            mod_prop_dict['delete']
        )
        return self.handle_replies(args, replies)
