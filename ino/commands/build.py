# -*- coding: utf-8; -*-

import re
import os.path
import inspect
import subprocess
import platform
import jinja2
import shlex

from jinja2.runtime import StrictUndefined

import ino.filters

from ino.commands.base import Command
from ino.environment import Version
from ino.filters import colorize
from ino.utils import SpaceList, list_subdirs
from ino.exc import Abort


class Build(Command):
    """
    Build a project in the current directory and produce a ready-to-upload
    firmware file.

    The project is expected to have a `src' subdirectory where all its sources
    are located. This directory is scanned recursively to find
    *.[c|cpp|pde|ino] files. They are compiled and linked into resulting
    firmware hex-file.

    Also any external library dependencies are tracked automatically. If a
    source file includes any library found among standard Arduino libraries or
    a library placed in `lib' subdirectory of the project, the library gets
    built too.

    Build artifacts are placed in `.build' subdirectory of the project.
    """

    name = 'build'
    help_line = "Build firmware from the current directory project"

    default_make = 'make'

    default_cppflags = ''
    default_cflags = ''
    default_cxxflags = ''
    default_ldflags = ''

    def setup_arg_parser(self, parser):
        super(Build, self).setup_arg_parser(parser)
        self.e.add_board_model_arg(parser)
        self.e.add_arduino_dist_arg(parser)

        parser.add_argument('--make', metavar='MAKE',
                            default=self.default_make,
                            help='Specifies the make tool to use. If '
                            'a full path is not given, searches in Arduino '
                            'directories before PATH. Default: %(default)s".')

        parser.add_argument('--cc', metavar='COMPILER',
                            default=None,
                            help='Specifies the compiler used for C files. If '
                            'a full path is not given, searches in Arduino '
                            'directories before PATH for the architecture '
                            'specific compiler.')

        parser.add_argument('--cxx', metavar='COMPILER',
                            default=None,
                            help='Specifies the compiler used for C++ files. '
                            'If a full path is not given, searches in Arduino '
                            'directories before PATH for the architecture '
                            'specific compiler.')

        parser.add_argument('--ar', metavar='AR',
                            default=None,
                            help='Specifies the AR tool to use. If a full path '
                            'is not given, searches in Arduino directories '
                            'before PATH for the architecture specific ar tool.')

        parser.add_argument('--objcopy', metavar='OBJCOPY',
                            default=None,
                            help='Specifies the OBJCOPY to use. If a full path '
                            'is not given, searches in Arduino directories '
                            'before PATH for the architecture specific objcopy '
                            'tool.')

        parser.add_argument('-f', '--cppflags', metavar='FLAGS',
                            default=self.default_cppflags,
                            help='Flags that will be passed to the compiler. '
                            'Note that multiple (space-separated) flags must '
                            'be surrounded by quotes, e.g. '
                            '`--cppflags="-DC1 -DC2"\' specifies flags to define '
                            'the constants C1 and C2. Default: "%(default)s".')

        parser.add_argument('--cflags', metavar='FLAGS',
                            default=self.default_cflags,
                            help='Like --cppflags, but the flags specified are '
                            'only passed to compilations of C source files. '
                            'Default: "%(default)s".')

        parser.add_argument('--cxxflags', metavar='FLAGS',
                            default=self.default_cxxflags,
                            help='Like --cppflags, but the flags specified '
                            'are only passed to compilations of C++ source '
                            'files. Default: "%(default)s".')

        parser.add_argument('--ldflags', metavar='FLAGS',
                            default=self.default_ldflags,
                            help='Like --cppflags, but the flags specified '
                            'are only passed during the linking stage. Note '
                            'these flags should be specified as if `ld\' were '
                            'being invoked directly (i.e. the `-Wl,\' prefix '
                            'should be omitted). Default: "%(default)s".')

        parser.add_argument('-v', '--verbose', default=False, action='store_true',
                            help='Verbose make output')

    def discover(self, args):
        board = self.e.board_model(args.board_model)

        self.e.find_arduino_dir('arduino_core_dir', 
                                ['hardware', 'arduino', board['arch'], 'cores', 'arduino'], 
                                ['Arduino.h'] if self.e.arduino_lib_version.major else ['WProgram.h'], 
                                'Arduino core library ({})'.format(board['arch']))

        self.e.find_arduino_dir('arduino_libraries_dir', ['libraries'],
                                human_name='Arduino standard libraries')

        if board['arch'] in ['sam']:
            self.e.find_arduino_dir('arduino_system_dir', ['hardware', 'arduino', board['arch'], 'system'],
                                    human_name='Arduino system libraries')

        if self.e.arduino_lib_version.major:
            self.e.find_arduino_dir('arduino_variants_dir',
                                    ['hardware', 'arduino', board['arch'], 'variants'],
                                    human_name='Arduino variants directory ({})'.format(board['arch']))

        toolset = [
            ('make', args.make, 'avr'),
            ('cc', args.cc, None),
            ('cxx', args.cxx, None),
            ('ar', args.ar, None),
            ('ld', None, None),
            ('objcopy', args.objcopy, None),
        ]

        tools_arch_mapping = {
            'avr': {
                'dirname': 'avr',
                'tool_prefix': 'avr-',
                'tools': {
                    'make': 'make',
                    'cc': 'gcc',
                    'cxx': 'g++',
                    'ar': 'ar',
                    'ld': 'gcc',
                    'objcopy': 'objcopy'
                }
            },
            'sam': {
                'dirname': 'g++_arm_none_eabi',
                'tool_prefix': 'arm-none-eabi-',
                'tools': {
                    'cc': 'gcc',
                    'cxx': 'g++',
                    'ar': 'ar',
                    'ld': 'g++',
                    'objcopy': 'objcopy'
                }
            }
        }

        if board['arch'] not in tools_arch_mapping:
            raise Abort('Unknown architecture "{}"'.format(board['arch']))

        arch_info = tools_arch_mapping[board['arch']]

        for tool_key, tool_binary, arch_override in toolset:
            actual_tool_binary = tool_binary if tool_binary else arch_info['tool_prefix'] + arch_info['tools'][tool_key]
            
            self.e.find_arduino_tool(
                tool_key, ['hardware', 'tools', arch_info['dirname'] if not arch_override else arch_override, 'bin'], 
                items=[actual_tool_binary], human_name=tool_binary)

    def setup_flags(self, args):
        board = self.e.board_model(args.board_model)

        mcu_key = '-mcpu=' if board['arch'] in ['sam'] else '-mmcu='
        mcu = mcu_key + board['build']['mcu']

        # Hard-code the flags that are essential to building the sketch
        self.e['cppflags'] = SpaceList([
            mcu,
            '-DF_CPU=' + board['build']['f_cpu'],
            '-DARDUINO=' + str(self.e.arduino_lib_version.as_int()),
            '-DARDUINO_' + board['build']['board'],
            '-DARDUINO_ARCH_' + board['arch'].upper(),
            '-I' + self.e['arduino_core_dir'],
        ]) 

        # Add additional flags as specified
        self.e['cppflags'] += SpaceList(shlex.split(args.cppflags))

        platform_settings = self.e.platform_settings()[board['arch']]
        self.e['cppflags'] += SpaceList(platform_settings['compiler']['cpp']['flags'].split(' '))

        self.e['objcopyflags'] = SpaceList(platform_settings['compiler']['elf2hex']['flags'].split(' '))

        # SAM boards have a pre-built system library
        if board['arch'] in ['sam']:
            system_dir = self.e.arduino_system_dir
            self.e['cppflags'] += [p.replace("{build.system.path}", system_dir) for p in
                ["-I{build.system.path}/libsam", 
                 "-I{build.system.path}/CMSIS/CMSIS/Include/", 
                 "-I{build.system.path}/CMSIS/Device/ATMEL/"]]

        if 'vid' in board['build']:
            self.e['cppflags'].append('-DUSB_VID=%s' % board['build']['vid'])
        if 'pid' in board['build']:
            self.e['cppflags'].append('-DUSB_PID=%s' % board['build']['pid'])
        if board['arch'] in ['sam']:
            self.e['cppflags'].append('-DUSBCON')
        
        if 'extra_flags' in board['build']:
            flags = [f.strip() for f in board['build']['extra_flags'].split(' ')]
            flags = filter(lambda f: f not in ['{build.usb_flags}'], flags)
            self.e['cppflags'].extend(flags)
            
        if self.e.arduino_lib_version.major:
            variant_dir = os.path.join(self.e.arduino_variants_dir, 
                                       board['build']['variant'])
            self.e.cppflags.append('-I' + variant_dir)

        self.e['cflags'] = SpaceList(shlex.split(args.cflags))

        self.e['cxxflags'] = SpaceList(shlex.split(args.cxxflags))

        # Again, hard-code the flags that are essential to building the sketch
        self.e['ldflags'] = SpaceList([mcu])
        self.e['ldflags'] += SpaceList([
            '-Wl,' + flag for flag in shlex.split(args.ldflags)
        ])

        self.e['ld_pre'] = ''
        self.e['ld_post'] = ''

        if board['arch'] in ['sam']:
            self.e['ldflags'] += SpaceList(['-mthumb', '-lgcc'])
            self.e['ld_pre'] = SpaceList([
                '-Wl,--check-sections',
                '-Wl,--gc-sections',
                '-Wl,--entry=Reset_Handler',
                '-Wl,--unresolved-symbols=report-all',
                '-Wl,--warn-common',
                '-Wl,--warn-section-align',
                '-Wl,--warn-unresolved-symbols',
                '-Wl,--start-group'
            ])

            # The order of linking is very specific in the SAM build.
            # This .o must come first, then the variant system lib,
            # then the project object files.
            self.e['ld_pre'] += SpaceList([
                os.path.join(self.e.build_dir, 'arduino', 'syscalls_sam3.o')
            ])

            self.e['ld_post'] = SpaceList([
                '-Wl,--end-group'
            ])
        
        if 'variant_system_lib' in board['build']:
            variant_system_lib = os.path.join(self.e.arduino_variants_dir, 
                                              board['build']['variant'],
                                              board['build']['variant_system_lib'])
            self.e['ld_variant_system_lib'] = variant_system_lib
        else:
            self.e['ld_variant_system_lib'] = ''

        if 'ldscript' in board['build']:
            ldscript = os.path.join(self.e.arduino_variants_dir, 
                                    board['build']['variant'],
                                    board['build']['ldscript'])
            self.e['ldflags'] += SpaceList([
                '-T' + ldscript
            ])

        self.e['names'] = {
            'obj': '%s.o',
            'lib': 'lib%s.a',
            'cpp': '%s.cpp',
            'deps': '%s.d',
        }

    def create_jinja(self, verbose):
        templates_dir = os.path.join(os.path.dirname(__file__), '..', 'make')
        self.jenv = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
            undefined=StrictUndefined, # bark on Undefined render
            extensions=['jinja2.ext.do'])

        # inject @filters from ino.filters
        for name, f in inspect.getmembers(ino.filters, lambda x: getattr(x, 'filter', False)):
            self.jenv.filters[name] = f

        # inject globals
        self.jenv.globals['e'] = self.e
        self.jenv.globals['v'] = '' if verbose else '@'
        self.jenv.globals['slash'] = os.path.sep
        self.jenv.globals['SpaceList'] = SpaceList

    def render_template(self, source, target, **ctx):
        template = self.jenv.get_template(source)
        contents = template.render(**ctx)
        out_path = os.path.join(self.e.build_dir, target)
        with open(out_path, 'wt') as f:
            f.write(contents)

        return out_path

    def make(self, makefile, **kwargs):
        makefile = self.render_template(makefile + '.jinja', makefile, **kwargs)
        ret = subprocess.call([self.e.make, '-f', makefile, 'all'])
        if ret != 0:
            raise Abort("Make failed with code %s" % ret)

    def recursive_inc_lib_flags(self, libdirs, board_arch):
        # These directories are not used in a build. For more info see
        # https://github.com/arduino/Arduino/wiki/Arduino-IDE-1.5:-Library-specification
        ignore_architectures = set(board['arch'] for board in self.e.board_models().itervalues()) - set([board_arch])
        lib_excludes = ['extras', 'examples']
        
        flags = SpaceList()
        for d in libdirs:
            flags.append('-I' + d)

            for subdir in list_subdirs(d, exclude=lib_excludes):
                # This dir requires special handling as it is architecture specific.
                # It is explained in more detail in the link above, but the expected
                # behavior is to prefer a subdir that matches the board architecture,
                # or if none is found, use the 'default' unoptimized architecture.
                if os.path.basename(subdir) == 'arch':
                    arch_subdir = list_subdirs(subdir, include=[board_arch])

                    if not arch_subdir:
                        arch_subdir = list_subdirs(subdir, include=['default'])

                    if arch_subdir:
                        flags.append('-I' + arch_subdir[0])
                        flags.extend('-I' + subd for subd in list_subdirs(arch_subdir[0], recursive=True, exclude=lib_excludes))
                else:
                    flags.append('-I' + subdir)
                    flags.extend('-I' + subd for subd in list_subdirs(subdir, recursive=True, exclude=lib_excludes))

        return flags

    def _scan_dependencies(self, dir, lib_dirs, inc_flags):
        output_filepath = os.path.join(self.e.build_dir, os.path.basename(dir), 'dependencies.d')
        self.make('Makefile.deps', inc_flags=inc_flags, src_dir=dir, output_filepath=output_filepath)
        self.e['deps'].append(output_filepath)

        # search for dependencies on libraries
        # for this scan dependency file generated by make
        # with regexes to find entries that start with
        # libraries dirname
        regexes = dict((lib, re.compile(r'\s' + lib + re.escape(os.path.sep))) for lib in lib_dirs)
        used_libs = set()
        with open(output_filepath) as f:
            for line in f:
                for lib, regex in regexes.iteritems():
                    if regex.search(line) and lib != dir:
                        used_libs.add(lib)

        return used_libs

    def scan_dependencies(self, args):
        board = self.e.board_model(args.board_model)
        board_arch = board['arch']

        self.e['deps'] = SpaceList()

        lib_dirs = [self.e.arduino_core_dir]
        lib_dirs += list_subdirs(self.e.lib_dir) 
        lib_dirs += list_subdirs(self.e.arduino_libraries_dir)
        lib_dirs += [os.path.join(self.e.arduino_variants_dir, board['build']['variant'])]

        inc_flags = self.recursive_inc_lib_flags(lib_dirs, board_arch)

        # If lib A depends on lib B it have to appear before B in final
        # list so that linker could link all together correctly
        # but order of `_scan_dependencies` is not defined, so...
        
        # 1. Get dependencies of sources in arbitrary order
        used_libs = list(self._scan_dependencies(self.e.src_dir, lib_dirs, inc_flags))

        # 2. Get dependencies of dependency libs themselves: existing dependencies
        # are moved to the end of list maintaining order, new dependencies are appended
        scanned_libs = set()
        while scanned_libs != set(used_libs):
            for lib in set(used_libs) - scanned_libs:
                dep_libs = self._scan_dependencies(lib, lib_dirs, inc_flags)

                i = 0
                for ulib in used_libs[:]:
                    if ulib in dep_libs:
                        # dependency lib used already, move it to the tail
                        used_libs.append(used_libs.pop(i))
                        dep_libs.remove(ulib)
                    else:
                        i += 1

                # append new dependencies to the tail
                used_libs.extend(dep_libs)
                scanned_libs.add(lib)

        self.e['used_libs'] = used_libs
        self.e['cppflags'].extend(self.recursive_inc_lib_flags(used_libs, board_arch))

    def run(self, args):
        self.discover(args)
        self.setup_flags(args)
        self.create_jinja(verbose=args.verbose)
        self.make('Makefile.sketch')
        self.scan_dependencies(args)
        self.make('Makefile')
