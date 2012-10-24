# BEGIN_COPYRIGHT
# 
# Copyright 2012 CRS4.
# 
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
# 
# END_COPYRIGHT

"""
Important environment variables
-------------------------------

The Pydoop setup looks in a number of default paths for what it
needs.  If necessary, you can override its behaviour or provide an
alternative path by exporting the environment variables below::

  JAVA_HOME, e.g., /opt/sun-jdk
  HADOOP_HOME, e.g., /opt/hadoop-1.0.2

Other relevant environment variables include::

  BOOST_PYTHON: name of the Boost.Python library, with the leading 'lib'
    and the trailing extension stripped. Defaults to 'boost_python'.
  HADOOP_VERSION, e.g., 0.20.2-cdh3u4 (override Hadoop's version string).
"""

import os, platform, re, glob, shutil
from distutils.core import setup
from distutils.extension import Extension
from distutils.command.build_ext import build_ext
from distutils.command.build_py import build_py
from distutils.command.clean import clean
from distutils.errors import DistutilsSetupError
from distutils import log

import pydoop
import pydoop.hadoop_utils as hu


try:
  JAVA_HOME = os.environ["JAVA_HOME"]
except KeyError:
  raise RuntimeError("java home not found, try setting JAVA_HOME")
HADOOP_HOME = pydoop.hadoop_home(fallback=None)
HADOOP_VERSION_INFO = pydoop.hadoop_version_info()
BOOST_PYTHON = os.getenv("BOOST_PYTHON", "boost_python")
PIPES_SRC = ["src/%s.cpp" % n for n in (
  "pipes",
  "pipes_context",
  "pipes_test_support",
  "pipes_serial_utils",
  "exceptions",
  "pipes_input_split",
  )]
HDFS_SRC = ["src/%s.cpp" % n for n in (
  "hdfs_fs",
  "hdfs_file",
  "hdfs_common",
  )]
PIPES_EXT_NAME = "_pipes"
HDFS_EXT_NAME = "_hdfs"


# ---------
# UTILITIES
# ---------

def get_arch():
  bits, _ = platform.architecture()
  if bits == "64bit":
    return "amd64", "64"
  return "i386", "32"


def get_java_include_dirs(java_home):
  p = platform.system().lower()  # Linux-specific
  java_inc = os.path.join(java_home, "include")
  java_platform_inc = "%s/%s" % (java_inc, p)
  return [java_inc, java_platform_inc]


def get_java_library_dirs(java_home):
  a = get_arch()[0]
  return [os.path.join(java_home, "jre/lib/%s/server" % a)]


def mtime(fn):
  return os.stat(fn).st_mtime


def must_generate(target, prerequisites):
  try:
    return max(mtime(p) for p in prerequisites) > mtime(target)
  except OSError:
    return True


def get_version_string(filename="VERSION"):
  try:
    with open(filename) as f:
      return f.read().strip()
  except IOError:
    raise DistutilsSetupError("failed to read version info")


def write_config(filename="pydoop/config.py"):
  prereq = "DEFAULT_HADOOP_HOME"
  if not os.path.exists(prereq):
    with open(prereq, "w") as f:
      f.write("%s\n" % HADOOP_HOME)
  if must_generate(filename, [prereq]):
    with open(filename, "w") as f:
      f.write("# GENERATED BY setup.py\n")
      f.write("DEFAULT_HADOOP_HOME='%s'\n" % HADOOP_HOME)


def write_version(filename="pydoop/version.py"):
  prereq = "VERSION"
  if must_generate(filename, [prereq]):
    version = get_version_string(filename=prereq)
    with open(filename, "w") as f:
      f.write("# GENERATED BY setup.py\n")
      f.write("version='%s'\n" % version)


def get_hdfs_macros(hdfs_hdr):
  """
  Search libhdfs headers for specific features.
  """
  hdfs_macros = []
  with open(hdfs_hdr) as f:
    t = f.read()
  delete_args = re.search(r"hdfsDelete\((.+)\)", t).groups()[0].split(",")
  cas_args = re.search(r"hdfsConnectAsUser\((.+)\)", t).groups()[0].split(",")
  ## cas_newinst = bool(re.search(r"hdfsConnectAsUserNewInstance\((.+)\)", t))
  ## c_newinst = bool(re.search(r"hdfsConnectNewInstance\((.+)\)", t))
  ## hflush = bool(re.search(r"hdfsHFlush\((.+)\)", t))
  if len(delete_args) > 2:
    hdfs_macros.append(("RECURSIVE_DELETE", None))
  if len(cas_args) > 3:
    hdfs_macros.append(("CONNECT_GROUP_INFO", None))
  ## if cas_newinst:
  ##   hdfs_macros.append(("CONNECT_AS_USER_NEW_INST", None))
  ## if c_newinst:
  ##   hdfs_macros.append(("CONNECT_NEW_INST", None))
  ## if hflush:
  ##   hdfs_macros.append(("HFLUSH", None))
  return hdfs_macros


def have_better_tls():
  """
  See ${HADOOP_HOME}/hadoop-hdfs-project/hadoop-hdfs/src/CMakeLists.txt
  """
  return False  # FIXME: need a portable implementation


def generate_hdfs_config(patched_src_dir):
  """
  Generate config.h for libhdfs.

  This is only relevant for recent Hadoop versions.
  """
  config_fn = os.path.join(patched_src_dir, "libhdfs", "config.h")
  with open(config_fn, "w") as f:
    f.write("#ifndef CONFIG_H\n#define CONFIG_H\n")
    if have_better_tls():
      f.write("#define HAVE_BETTER_TLS\n")
    f.write("#endif\n")


class HadoopSourcePatcher(object):

  def __init__(self, hadoop_version_info=HADOOP_VERSION_INFO):
    hadoop_tag = "hadoop-%s" % hadoop_version_info
    self.patch_fn = "patches/%s.patch" % hadoop_tag
    self.src_dir = "src/%s" % hadoop_tag
    self.patched_src_dir = "%s.patched" % self.src_dir
    self.from_jtree = os.path.join(
      self.patched_src_dir, "org/apache/hadoop/mapred/pipes"
      )
    self.to_jtree = os.path.join(
      self.patched_src_dir, "it/crs4/pydoop/pipes"
      )

  def convert_pkg(self):
    assert os.path.isdir(self.from_jtree)
    os.makedirs(self.to_jtree)
    for bn in os.listdir(self.from_jtree):
      with open(os.path.join(self.from_jtree, bn)) as f:
        content = f.read()
      with open(os.path.join(self.to_jtree, bn), "w") as f:
        f.write(content.replace(
          "org.apache.hadoop.mapred.pipes", " it.crs4.pydoop.pipes"
          ))

  def patch(self):
    if must_generate(self.patched_src_dir, [self.src_dir, self.patch_fn]):
      log.info("patching source code %r" % (self.src_dir,))
      shutil.rmtree(self.patched_src_dir, ignore_errors=True)
      shutil.copytree(self.src_dir, self.patched_src_dir)
      cmd = "patch -d %s -N -p1 < %s" % (self.patched_src_dir, self.patch_fn)
      if os.system(cmd):
        raise DistutilsSetupError("Error applying patch.  Command: %s" % cmd)
      self.convert_pkg()
    return self.patched_src_dir


def create_pipes_ext(patched_src_dir, hadoop_vinfo=None):
  if hadoop_vinfo is None:
    hadoop_vinfo = HADOOP_VERSION_INFO
  include_dirs = ["%s/%s/api" % (patched_src_dir, _) for _ in "pipes", "utils"]
  libraries = ["pthread", BOOST_PYTHON]
  if hadoop_vinfo.tuple != (0, 20, 2):
    libraries.append("ssl")
  return BoostExtension(
    pydoop.complete_mod_name(PIPES_EXT_NAME, hadoop_vinfo=hadoop_vinfo),
    PIPES_SRC,
    glob.glob("%s/*/impl/*.cc" % patched_src_dir),
    include_dirs=include_dirs,
    libraries=libraries
    )


def create_hdfs_ext(patched_src_dir):
  generate_hdfs_config(patched_src_dir)
  java_include_dirs = get_java_include_dirs(JAVA_HOME)
  log.info("java_include_dirs: %r" % (java_include_dirs,))
  include_dirs = java_include_dirs + ["%s/libhdfs" % patched_src_dir]
  java_library_dirs = get_java_library_dirs(JAVA_HOME)
  log.info("java_library_dirs: %r" % (java_library_dirs,))
  return BoostExtension(
    pydoop.complete_mod_name(HDFS_EXT_NAME),
    HDFS_SRC,
    glob.glob("%s/libhdfs/*.c" % patched_src_dir),
    include_dirs=include_dirs,
    library_dirs=java_library_dirs,
    runtime_library_dirs=java_library_dirs,
    libraries=["pthread", BOOST_PYTHON, "jvm"],
    define_macros=get_hdfs_macros(
      os.path.join(patched_src_dir, "libhdfs", "hdfs.h")
      ),
    )


# ---------------------------------------
# Custom distutils extension and commands
# ---------------------------------------

class BoostExtension(Extension):
  """
  Customized Extension class that generates the necessary Boost.Python
  export code.
  """
  export_pattern = re.compile(r"void\s+export_(\w+)")

  def __init__(self, name, wrap_sources, aux_sources, **kw):
    Extension.__init__(self, name, wrap_sources+aux_sources, **kw)
    self.module_name = self.name.split(".", 1)[-1]
    self.wrap_sources = wrap_sources

  def generate_main(self):
    destdir = os.path.split(self.wrap_sources[0])[0]  # should be ok
    outfn = os.path.join(destdir, "%s_main.cpp" % self.module_name)
    if must_generate(outfn, self.wrap_sources):
      log.debug("generating main for %s\n" % self.name)
      first_half = ["#include <boost/python.hpp>"]
      second_half = ["BOOST_PYTHON_MODULE(%s){" % self.module_name]
      for fn in self.wrap_sources:
        with open(fn) as f:
          code = f.read()
        m = self.export_pattern.search(code)
        if m is not None:
          fun_name = "export_%s" % m.groups()[0]
          first_half.append("void %s();" % fun_name)
          second_half.append("%s();" % fun_name)
      second_half.append("}")
      with open(outfn, "w") as outf:
        for line in first_half:
          outf.write("%s%s" % (line, os.linesep))
        for line in second_half:
          outf.write("%s%s" % (line, os.linesep))
    return outfn


class JavaLib(object):
  """
  Encapsulates information needed to build a Java library.
  """
  def __init__(self, hadoop_vinfo=HADOOP_VERSION_INFO):
    self.hadoop_vinfo = hadoop_vinfo
    self.jar_name = pydoop.jar_name(self.hadoop_vinfo)
    self.classpath = pydoop.hadoop_classpath()
    if not self.classpath:
      log.warn("could not set classpath, java code may not compile")
    self.java_files = ["src/it/crs4/pydoop/NoSeparatorTextOutputFormat.java"]
    if self.hadoop_vinfo.has_security():
      if hadoop_vinfo.cdh >= (4, 0, 0) and not hadoop_vinfo.ext:
        return  # TODO: add support for mrv2
      # add our fix for https://issues.apache.org/jira/browse/MAPREDUCE-4000
      self.java_files.extend(glob.glob(
        "%s/*" % HadoopSourcePatcher(self.hadoop_vinfo).to_jtree
        ))


class BuildExt(build_ext):

  def finalize_options(self):
    build_ext.finalize_options(self)
    patched_src_dir = HadoopSourcePatcher(HADOOP_VERSION_INFO).patch()
    self.extensions = [
      create_pipes_ext(patched_src_dir),
      create_hdfs_ext(patched_src_dir),
      ]
    self.java_libs = [JavaLib(HADOOP_VERSION_INFO)]
    if HADOOP_VERSION_INFO.cdh >= (4, 0, 0):
      hadoop_vinfo = hu.cdh_mr1_version(HADOOP_VERSION_INFO)
      patched_src_dir = HadoopSourcePatcher(hadoop_vinfo).patch()
      self.extensions.append(create_pipes_ext(patched_src_dir, hadoop_vinfo))
      self.java_libs.append(JavaLib(hadoop_vinfo))
    for e in self.extensions:
      e.sources.append(e.generate_main())

  def build_extension(self, ext):
    try:
      self.compiler.compiler_so.remove("-Wstrict-prototypes")
    except ValueError:
      pass
    build_ext.build_extension(self, ext)

  def run(self):
    log.info("hadoop_home: %r" % (HADOOP_HOME,))
    log.info("hadoop_version: '%s'" % HADOOP_VERSION_INFO)
    log.info("java_home: %r" % (JAVA_HOME,))
    build_ext.run(self)
    for jlib in self.java_libs:
      self.__build_java_lib(jlib)

  def __build_java_lib(self, jlib):
    log.info("Building java code for hadoop-%s" % jlib.hadoop_vinfo)
    compile_cmd = "javac -classpath %s" % jlib.classpath
    class_dir = os.path.join(self.build_temp, "pipes-%s" % jlib.hadoop_vinfo)
    package_path = os.path.join(self.build_lib, "pydoop", jlib.jar_name)
    if not os.path.exists(class_dir):
      os.mkdir(class_dir)
    compile_cmd += " -d '%s'" % class_dir
    log.info("Compiling Java classes")
    for f in jlib.java_files:
      compile_cmd += " %s" % f
    log.debug("Command: %s", compile_cmd)
    ret = os.system(compile_cmd)
    if ret:
      raise DistutilsSetupError(
        "Error compiling java component.  Command: %s" % compile_cmd
        )
    package_cmd = "jar -cf %(package_path)s -C %(class_dir)s ./it" % {
      'package_path': package_path, 'class_dir': class_dir
      }
    log.info("Packaging Java classes")
    log.debug("Command: %s", package_cmd)
    ret = os.system(package_cmd)
    if ret:
      raise DistutilsSetupError(
        "Error packaging java component.  Command: %s" % package_cmd
        )


class Clean(clean):
  """
  Custom clean action that removes files generated by the build process.
  """
  def run(self):
    clean.run(self)
    this_dir = os.path.dirname(os.path.realpath(__file__))
    shutil.rmtree(os.path.join(this_dir, 'dist'), ignore_errors=True)
    pydoop_src_path = os.path.join(this_dir, 'src')
    r = re.compile('(%s|%s)_.*_main.cpp$' % (HDFS_EXT_NAME, PIPES_EXT_NAME))
    paths = filter(r.search, os.listdir(pydoop_src_path))
    absolute_paths = [os.path.join(pydoop_src_path, f) for f in paths]
    for f in absolute_paths:
      if not self.dry_run:
        try:
          if os.path.exists(f):
            os.remove(f)
        except OSError as e:
          log.warn("Error removing file: %s" % e)


class BuildPy(build_py):

  def run(self):
    write_config()
    write_version()
    build_py.run(self)


setup(
  name="pydoop",
  version=get_version_string(),
  description=pydoop.__doc__.strip().splitlines()[0],
  long_description=pydoop.__doc__.lstrip(),
  author=pydoop.__author__,
  author_email=pydoop.__author_email__,
  url=pydoop.__url__,
  download_url="https://sourceforge.net/projects/pydoop/files/",
  packages=[
    "pydoop",
    "pydoop.hdfs",
    "pydoop.app",
    ],
  cmdclass={
    "build_py": BuildPy,
    "build_ext": BuildExt,
    "clean": Clean,
    },
  ext_modules=[None],  # just to trigger build_ext
  scripts=["scripts/pydoop"],
  platforms=["Linux"],
  license="Apache-2.0",
  keywords=["hadoop", "mapreduce"],
  classifiers=[
    "Programming Language :: Python",
    "License :: OSI Approved :: Apache Software License",
    "Operating System :: POSIX :: Linux",
    "Topic :: Software Development :: Libraries :: Application Frameworks",
    "Intended Audience :: Developers",
    ],
  )

# vim: set sw=2 ts=2 et
